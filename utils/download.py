import hashlib
import html
import json
import logging
import os
import os.path
import random
import shutil
import time
import zipfile
from pathlib import Path
from threading import Thread
from urllib.parse import urlparse

import bs4
import cv2
import requests
from PIL import Image
from PIL import UnidentifiedImageError
from sqlalchemy import and_
from user_agent import generate_user_agent

import config
from bitchan_client import DaemonCom
from database.models import GlobalSettings
from database.models import Messages
from database.models import UploadSites
from database.utils import session_scope
from utils.encryption import crypto_multi_decrypt
from utils.files import LF
from utils.files import count_files_in_zip
from utils.files import data_file_multiple_insert
from utils.files import delete_file
from utils.files import delete_files_recursive
from utils.files import extract_zip
from utils.files import generate_thumbnail
from utils.files import human_readable_size
from utils.general import get_random_alphanumeric_string
from utils.shared import regenerate_thread_card_and_popup
from utils.steg import check_steg

DB_PATH = 'sqlite:///' + config.DATABASE_BITCHAN

logger = logging.getLogger('bitchan.download')
daemon_com = DaemonCom()


def generate_hash(file_path):
    """
    Generates an SHA256 hash value from a file

    :param file_path: path to the file for hash validation
    :type file_path: string
    """
    m = hashlib.sha256()
    with open(file_path, 'rb') as f:
        while True:
            chunk = f.read(1000 * 1000)  # 1MB
            if not chunk:
                break
            m.update(chunk)
    return m.hexdigest()


def validate_file(file_path, hash):
    """
    Validates a file against an SHA256 hash value

    :param file_path: path to the file for hash validation
    :type file_path:  string
    :param hash:      expected hash value of the file
    :type hash:       string -- SHA256 hash value
    """
    m = hashlib.sha256()
    with open(file_path, 'rb') as f:
        while True:
            chunk = f.read(1000 * 1000)  # 1MB
            if not chunk:
                break
            m.update(chunk)
    return m.hexdigest() == hash


def report_downloaded_amount(message_id, download_path, file_size):
    try:
        with session_scope(DB_PATH) as new_session:
            message = new_session.query(Messages).filter(
                Messages.message_id == message_id).first()
            if message:
                timer = time.time()
                while os.path.exists(download_path):
                    if timer < time.time():
                        while timer < time.time():
                            timer += 3
                        downloaded_size = os.path.getsize(download_path)
                        message.file_progress = "{}/{} downloaded ({:.1f} %)".format(
                            human_readable_size(downloaded_size),
                            human_readable_size(file_size),
                            (downloaded_size / file_size) * 100)
                        new_session.commit()
                    time.sleep(1)
    except:
        logger.error("Exception while reporting file size")


def allow_download(message_id):
    try:
        logger.info("{}: Allowing download".format(message_id[-config.ID_LENGTH:].upper()))
        with session_scope(DB_PATH) as new_session:
            message = new_session.query(Messages).filter(
                Messages.message_id == message_id).first()
            if message:
                file_path = "{}/{}".format(
                    config.FILE_DIRECTORY, message.saved_file_filename)

                # Pick a download slot to fill (2 slots per domain)
                domain = urlparse(message.file_url).netloc
                lockfile1 = "/var/lock/upload_{}_1.lock".format(domain)
                lockfile2 = "/var/lock/upload_{}_2.lock".format(domain)

                lf = LF()
                lockfile = random.choice([lockfile1, lockfile2])
                if lf.lock_acquire(lockfile, to=600):
                    try:
                        (file_download_successful,
                         file_size,
                         file_amount,
                         file_do_not_download,
                         file_sha256_hashes_match,
                         file_progress,
                         media_info,
                         message_steg) = download_and_extract(
                            message.thread.chan.address,
                            message_id,
                            message.file_url,
                            json.loads(message.file_upload_settings),
                            json.loads(message.file_extracts_start_base64),
                            message.upload_filename,
                            file_path,
                            message.file_sha256_hash,
                            message.file_enc_cipher,
                            message.file_enc_key_bytes,
                            message.file_enc_password)
                    finally:
                        lf.lock_release(lockfile)

                if file_download_successful:
                    if file_size:
                        message.file_size = file_size
                    if file_amount:
                        message.file_amount = file_amount
                    message.file_download_successful = file_download_successful
                    message.file_do_not_download = file_do_not_download
                    message.file_sha256_hashes_match = file_sha256_hashes_match
                    message.media_info = json.dumps(media_info)
                    message.message_steg = json.dumps(message_steg)

                    regenerate_thread_card_and_popup(
                        thread_hash=message.thread.thread_hash,
                        message_id=message_id)
                else:
                    message.file_progress = file_progress

                new_session.commit()

    except Exception as e:
        logger.error("{}: Error allowing download: {}".format(message_id[-config.ID_LENGTH:].upper(), e))
    finally:
        with session_scope(DB_PATH) as new_session:
            message = new_session.query(Messages).filter(
                Messages.message_id == message_id).first()
            message.file_currently_downloading = False
            new_session.commit()


def download_with_resume(message_id, url, file_path, hash=None, timeout=15):
    """
    Performs a HTTP(S) download that can be restarted if prematurely terminated.
    The HTTP server must support byte ranges.
    From https://gist.github.com/idolpx/921fc79368903d3a90800ef979abb787
    """
    # don't download if the file exists
    if os.path.exists(file_path):
        return
    first_byte = 0
    block_size = 1000 * 500  # 0.5MB
    tmp_file_path = "{}.part".format(file_path)
    if os.path.exists(tmp_file_path):
        first_byte = os.path.getsize(tmp_file_path)
    file_mode = 'ab' if first_byte else 'wb'
    if first_byte:
        logger.info('{}: Resuming download from {:.1f} MB'.format(
            message_id[-config.ID_LENGTH:].upper(), first_byte / 1e6))
    else:
        logger.info('{}: Starting download'.format(message_id[-config.ID_LENGTH:].upper()))
    file_size = -1
    try:
        file_size = int(requests.head(
            url,
            headers={'User-Agent': generate_user_agent()}).headers['Content-length'])
        logger.debug('{}: File size is {}'.format(message_id[-config.ID_LENGTH:].upper(), file_size))

        if not os.path.exists(tmp_file_path):
            Path(tmp_file_path).touch()
        thread_download = Thread(
            target=report_downloaded_amount, args=(message_id, tmp_file_path, file_size,))
        thread_download.daemon = True
        thread_download.start()

        headers = {
            "Range": "bytes={}-".format(first_byte),
            'User-Agent': generate_user_agent()
        }
        r = requests.get(
            url,
            proxies=config.TOR_PROXIES,
            headers=headers,
            stream=True,
            timeout=timeout)
        with open(tmp_file_path, file_mode) as f:
            for chunk in r.iter_content(chunk_size=block_size):
                if chunk:  # filter out keep-alive new chunks
                    f.write(chunk)
    except IOError as e:
        logger.error('{}: IO Error: {}'.format(message_id[-config.ID_LENGTH:].upper(), e))
    except Exception as e:
        logger.error('{}: Error: {}'.format(message_id[-config.ID_LENGTH:].upper(), e))
    finally:
        # rename the temp download file to the correct name if fully downloaded
        if file_size == os.path.getsize(tmp_file_path):
            # if there's a hash value, validate the file
            if hash and not validate_file(tmp_file_path, hash):
                raise Exception('{}: Error validating the file against its SHA256 hash'.format(
                    message_id[-config.ID_LENGTH:].upper()))
            shutil.move(tmp_file_path, file_path)
            return file_path
        elif file_size == -1:
            logger.error('{}: Error getting Content-Length from server: {}'.format(
                message_id[-config.ID_LENGTH:].upper(), url))


def download_and_extract(
        address,
        message_id,
        file_url,
        file_upload_settings,
        file_extracts_start_base64,
        upload_filename,
        file_path,
        file_sha256_hash,
        file_enc_cipher,
        file_enc_key_bytes,
        file_enc_password):

    logger.info("download_and_extract {}, {}, {}, {}, {}, {}, {}, {}, {}, password={}".format(
        address,
        message_id,
        file_url,
        file_upload_settings,
        upload_filename,
        file_path,
        file_sha256_hash,
        file_enc_cipher,
        file_enc_key_bytes,
        file_enc_password))

    file_sha256_hashes_match = False
    file_size = None
    file_amount = None
    file_do_not_download = None
    file_progress = None
    file_download_successful = None
    downloaded = None
    force_allow_download = False
    download_url = None
    media_info = {}
    message_steg = {}
    resume_start_download = False

    if message_id in daemon_com.get_start_download() or resume_start_download:
        resume_start_download = True
        force_allow_download = True
        file_do_not_download = False
        daemon_com.remove_start_download(message_id)

    # save downloaded file to /tmp/
    # filename has been randomly generated, so no risk of collisions
    download_path = "/tmp/{}".format(upload_filename)

    with session_scope(DB_PATH) as new_session:
        settings = new_session.query(GlobalSettings).first()

        if (file_enc_cipher == "NONE" and
                settings.never_auto_download_unencrypted and
                not force_allow_download):
            logger.info(
                "{}: Instructed to never auto-download unencrypted attachments. "
                "Manual override needed.".format(
                    message_id[-config.ID_LENGTH:].upper()))
            file_do_not_download = True
            message = new_session.query(Messages).filter(
                Messages.message_id == message_id).first()
            file_progress = "Current settings prohibit automatically downloading unencrypted attachments."
            if message:
                message.file_progress = "Current settings prohibit automatically downloading unencrypted attachments."
                new_session.commit()
            return (file_download_successful,
                    file_size,
                    file_amount,
                    file_do_not_download,
                    file_sha256_hashes_match,
                    file_progress,
                    media_info,
                    message_steg)

        if (not settings.auto_dl_from_unknown_upload_sites and
                not is_upload_site_in_database(file_upload_settings)):
            logger.info(
                "{}: Instructed to never auto-download from unknown upload sites. "
                "Save upload site to database then instruct to download.".format(
                    message_id[-config.ID_LENGTH:].upper()))
            file_do_not_download = True
            message = new_session.query(Messages).filter(
                Messages.message_id == message_id).first()
            file_progress = "Unknown upload site detected. Add upload site and manually start download."
            if message:
                message.file_progress = file_progress
                new_session.commit()
            return (file_download_successful,
                    file_size,
                    file_amount,
                    file_do_not_download,
                    file_sha256_hashes_match,
                    file_progress,
                    media_info,
                    message_steg)

        if not settings.allow_net_file_size_check and not force_allow_download:
            logger.info("{}: Not connecting to determine file size. Manual override needed.".format(
                message_id[-config.ID_LENGTH:].upper()))
            file_do_not_download = True
            message = new_session.query(Messages).filter(
                Messages.message_id == message_id).first()
            file_progress = "Configuration doesn't allow getting file size. Manual override required."
            if message:
                message.file_progress = file_progress
                new_session.commit()
            return (file_download_successful,
                    file_size,
                    file_amount,
                    file_do_not_download,
                    file_sha256_hashes_match,
                    file_progress,
                    media_info,
                    message_steg)
        else:
            logger.info("{}: Getting URL and file size...".format(message_id[-config.ID_LENGTH:].upper()))

    # Parse page for URL to direct download zip
    if "direct_dl_url" in file_upload_settings and file_upload_settings["direct_dl_url"]:
        download_url = file_url
    else:
        try:
            logger.info("{}: Finding download URL on upload page".format(message_id[-config.ID_LENGTH:].upper()))
            html_return = requests.get(
                file_url,
                headers={'User-Agent': generate_user_agent()})
            soup = bs4.BeautifulSoup(html_return.text, "html.parser")
            for link in soup.find_all('a', href=True):
                href = link.get('href')
                if href and href.endswith(upload_filename):
                    download_url = href
                    break
        except:
            logger.error("{}: Error getting upload page".format(message_id[-config.ID_LENGTH:].upper()))

    if not download_url:
        logger.error("{}: Could not find URL for {}".format(
            message_id[-config.ID_LENGTH:].upper(), upload_filename))
        daemon_com.remove_start_download(message_id)
        with session_scope(DB_PATH) as new_session:
            message = new_session.query(Messages).filter(
                Messages.message_id == message_id).first()
            if message:
                message.file_progress = "Could not find download URL. Try again."
                new_session.commit()
        return (file_download_successful,
                file_size,
                file_amount,
                file_do_not_download,
                file_sha256_hashes_match,
                file_progress,
                media_info,
                message_steg)
    else:
        logger.info("{}: Found URL".format(message_id[-config.ID_LENGTH:].upper()))
        time.sleep(5)
        for _ in range(3):
            logger.info("{}: Getting file size".format(message_id[-config.ID_LENGTH:].upper()))
            try:
                if resume_start_download:
                    headers = requests.head(
                        download_url,
                        headers={'User-Agent': generate_user_agent()}).headers
                    logger.info("{}: Headers: {}".format(message_id[-config.ID_LENGTH:].upper(), headers))
                    if 'Content-length' in headers:
                        file_size = int(headers['Content-length'])
                        logger.info("{}: File size acquired: {}".format(
                            message_id[-config.ID_LENGTH:].upper(), human_readable_size(file_size)))
                        break
                    else:
                        logger.error("{}: 'content-length' not in header".format(message_id[-config.ID_LENGTH:].upper()))
                else:
                    with session_scope(DB_PATH) as new_session:
                        settings = new_session.query(GlobalSettings).first()
                        # Don't download file if user set to 0
                        if settings.max_download_size == 0:
                            downloaded = "prohibited"
                            file_do_not_download = True
                            logger.info("{}: File prevented from being auto-download.".format(
                                message_id[-config.ID_LENGTH:].upper()))
                            break

                        # Check file size and auto-download if less than user-set size
                        headers = requests.head(
                            download_url,
                            headers={'User-Agent': generate_user_agent()}).headers
                        logger.info("{}: Headers: {}".format(message_id[-config.ID_LENGTH:].upper(), headers))
                        if 'Content-length' in headers:
                            file_size = int(headers['Content-length'])
                            if file_size and file_size > settings.max_download_size * 1024 * 1024:
                                downloaded = "too_large"
                                file_do_not_download = True
                                logger.info(
                                    "{}: File size ({}) is greater than max allowed "
                                    "to auto-download ({}). Not downloading.".format(
                                        message_id[-config.ID_LENGTH:].upper(),
                                        human_readable_size(file_size),
                                        human_readable_size(settings.max_download_size * 1024 * 1024)))
                                break
                            else:
                                file_do_not_download = False
                                logger.info(
                                    "{}: File size ({}) is less than max allowed "
                                    "to auto-download ({}). Downloading.".format(
                                        message_id[-config.ID_LENGTH:].upper(),
                                        human_readable_size(file_size),
                                        human_readable_size(settings.max_download_size * 1024 * 1024)))
                                break
                        else:
                            logger.error("{}: 'content-length' not in header".format(
                                message_id[-config.ID_LENGTH:].upper()))
                time.sleep(15)
            except Exception as err:
                logger.exception("{}: Could not get file size: {}".format(
                    message_id[-config.ID_LENGTH:].upper(), err))
                file_do_not_download = True
                time.sleep(15)

        if file_do_not_download and not force_allow_download:
            logger.info("{}: Not downloading.".format(message_id[-config.ID_LENGTH:].upper()))
            with session_scope(DB_PATH) as new_session:
                message = new_session.query(Messages).filter(
                    Messages.message_id == message_id).first()
                if message:
                    message.file_progress = "Configuration doesn't allow auto-downloading of this file. Manual override required."
                    new_session.commit()
            return (file_download_successful,
                    file_size,
                    file_amount,
                    file_do_not_download,
                    file_sha256_hashes_match,
                    file_progress,
                    media_info,
                    message_steg)
        else:
            logger.info("{}: Downloading...".format(message_id[-config.ID_LENGTH:].upper()))
            file_do_not_download = False
            time.sleep(5)

        for _ in range(config.DOWNLOAD_ATTEMPTS):
            try:
                download_with_resume(message_id, download_url, download_path)
                if file_size == os.path.getsize(download_path):
                    break
                logger.error("{}: File size does not match what's expected".format(message_id[-config.ID_LENGTH:].upper()))
            except IOError:
                logger.error("{}: Could not download".format(message_id[-config.ID_LENGTH:].upper()))
            except Exception as err:
                logger.error("{}: Exception downloading: {}".format(message_id[-config.ID_LENGTH:].upper(), err))
            time.sleep(60)

        try:
            if file_size == os.path.getsize(download_path):
                logger.info("{}: Download completed".format(message_id[-config.ID_LENGTH:].upper()))
                downloaded = "downloaded"
            else:
                logger.error("{}: Download not complete".format(message_id[-config.ID_LENGTH:].upper()))
        except:
            logger.error("{}: Issue downloading file".format(message_id[-config.ID_LENGTH:].upper()))

        if downloaded == "prohibited":
            logger.info("{}: File prohibited from auto-downloading".format(
                message_id[-config.ID_LENGTH:].upper()))
        elif downloaded == "too_large":
            with session_scope(DB_PATH) as new_session:
                settings = new_session.query(GlobalSettings).first()
                logger.info("{}: File size ({}) is larger than allowed to auto-download ({})".format(
                    message_id[-config.ID_LENGTH:].upper(),
                    human_readable_size(file_size),
                    human_readable_size(settings.max_download_size * 1024 * 1024)))
        elif downloaded == "downloaded":
            logger.info("{}: File successfully downloaded".format(message_id[-config.ID_LENGTH:].upper()))
            file_download_successful = True
        elif downloaded is None:
            logger.error("{}: Could not download file after {} attempts".format(
                message_id[-config.ID_LENGTH:].upper(), config.DOWNLOAD_ATTEMPTS))
            with session_scope(DB_PATH) as new_session:
                message = new_session.query(Messages).filter(
                    Messages.message_id == message_id).first()
                if message:
                    message.file_progress = "Could not download file after {} attempts".format(
                        config.DOWNLOAD_ATTEMPTS)
                    new_session.commit()
            file_download_successful = False

        if file_download_successful:
            # Add missing parts back to file
            if file_extracts_start_base64:
                size_before = os.path.getsize(download_path)
                data_file_multiple_insert(download_path, file_extracts_start_base64, chunk=4096)
                logger.info("{}: File data insertion. Before: {}, After: {}".format(
                    message_id[-config.ID_LENGTH:].upper(), size_before, os.path.getsize(download_path)))

            # compare SHA256 hashes
            if file_sha256_hash:
                if not validate_file(download_path, file_sha256_hash):
                    logger.info(
                        "{}: File SHA256 hash ({}) does not match provided SHA256"
                        " hash ({}). Deleting.".format(
                            message_id[-config.ID_LENGTH:].upper(),
                            generate_hash(download_path),
                            file_sha256_hash))
                    file_sha256_hashes_match = False
                    file_download_successful = False
                    delete_file(download_path)
                    return (file_download_successful,
                            file_size,
                            file_amount,
                            file_do_not_download,
                            file_sha256_hashes_match,
                            file_progress,
                            media_info,
                            message_steg)
                else:
                    file_sha256_hashes_match = True
                    logger.info("{}: File SHA256 hashes match ({})".format(
                        message_id[-config.ID_LENGTH:].upper(), file_sha256_hash))

            if file_enc_cipher == "NONE":
                logger.info("{}: File not encrypted".format(message_id[-config.ID_LENGTH:].upper()))
                full_path_filename = download_path
            else:
                # decrypt file
                full_path_filename = "/tmp/{}.zip".format(
                    get_random_alphanumeric_string(12, with_punctuation=False, with_spaces=False))
                delete_file(full_path_filename)  # make sure no file already exists
                logger.info("{}: Decrypting file".format(message_id[-config.ID_LENGTH:].upper()))
                with session_scope(DB_PATH) as new_session:
                    message = new_session.query(Messages).filter(
                        Messages.message_id == message_id).first()
                    if message:
                        message.file_progress = "Decrypting file"
                        new_session.commit()

                try:
                    with session_scope(DB_PATH) as new_session:
                        settings = new_session.query(GlobalSettings).first()
                        ret_crypto = crypto_multi_decrypt(
                            file_enc_cipher,
                            file_enc_password + config.PGP_PASSPHRASE_ATTACH,
                            download_path,
                            full_path_filename,
                            key_bytes=file_enc_key_bytes,
                            max_size_bytes=settings.max_extract_size * 1024 * 1024)
                        if not ret_crypto:
                            logger.error("{}: Issue decrypting attachment")
                            message = new_session.query(Messages).filter(
                                Messages.message_id == message_id).first()
                            if message:
                                message.file_progress = "Issue decrypting attachment. Check log."
                                new_session.commit()
                            file_download_successful = False
                            return (file_download_successful,
                                    file_size,
                                    file_amount,
                                    file_do_not_download,
                                    file_sha256_hashes_match,
                                    file_progress,
                                    media_info,
                                    message_steg)
                    logger.info("{}: Finished decrypting file".format(message_id[-config.ID_LENGTH:].upper()))

                    # z = zipfile.ZipFile(download_path)
                    # z.setpassword(config.PGP_PASSPHRASE_ATTACH.encode())
                    # z.extract(extract_filename, path=extract_path)
                except Exception:
                    logger.exception("Error decrypting attachment")
                    message = new_session.query(Messages).filter(
                        Messages.message_id == message_id).first()
                    if message:
                        message.file_progress = "Error decrypting attachment. Check log."
                        new_session.commit()

            # Get the number of files in the zip archive
            try:
                file_amount_test = count_files_in_zip(message_id, full_path_filename)
            except Exception as err:
                with session_scope(DB_PATH) as new_session:
                    message = new_session.query(Messages).filter(
                        Messages.message_id == message_id).first()
                    if message:
                        message.file_progress = "Error checking zip: {}".format(
                            message_id[-config.ID_LENGTH:].upper(), err)
                        new_session.commit()
                logger.error("{}: Error checking zip: {}".format(
                    message_id[-config.ID_LENGTH:].upper(), err))
                file_do_not_download = True
                return (file_download_successful,
                        file_size,
                        file_amount,
                        file_do_not_download,
                        file_sha256_hashes_match,
                        file_progress,
                        media_info,
                        message_steg)

            if file_amount_test:
                file_amount = file_amount_test

            if file_amount and file_amount > config.FILE_ATTACHMENTS_MAX:
                logger.info("{}: Number of attachments ({}) exceed the maximum ({}).".format(
                    message_id[-config.ID_LENGTH:].upper(), file_amount, config.FILE_ATTACHMENTS_MAX))
                file_do_not_download = True
                return (file_download_successful,
                        file_size,
                        file_amount,
                        file_do_not_download,
                        file_sha256_hashes_match,
                        file_progress,
                        media_info,
                        message_steg)

            # Check size of zip contents before extraction
            can_extract = True
            with zipfile.ZipFile(full_path_filename, 'r') as zipObj:
                total_size = 0
                for each_file in zipObj.infolist():
                    total_size += each_file.file_size
                logger.info("ZIP contents size: {}".format(total_size))
                with session_scope(DB_PATH) as new_session:
                    settings = new_session.query(GlobalSettings).first()
                    if (settings.max_extract_size and
                            total_size > settings.max_extract_size * 1024 * 1024):
                        can_extract = False
                        logger.error(
                            "ZIP content size greater than max allowed ({} bytes). " 
                            "Not extracting.".format(settings.max_extract_size * 1024 * 1024))
                        file_download_successful = False
                        with session_scope(DB_PATH) as new_session:
                            message = new_session.query(Messages).filter(
                                Messages.message_id == message_id).first()
                            if message:
                                message.file_progress = "Attachment extraction size greater than allowed"
                                new_session.commit()

            if can_extract:
                # Extract zip archive
                extract_path = "{}/{}".format(config.FILE_DIRECTORY, message_id)
                extract_zip(message_id, full_path_filename, extract_path)
                delete_file(full_path_filename)  # Secure delete

                errors_files, media_info, message_steg = process_attachments(message_id, extract_path)

                if errors_files:
                    logger.error(
                        "{}: File extension greater than {} characters. Deleting.".format(
                            message_id[-config.ID_LENGTH:].upper(), config.MAX_FILE_EXT_LENGTH))
                    delete_files_recursive(extract_path)
                    file_do_not_download = True
                    return (file_download_successful,
                            file_size,
                            file_amount,
                            file_do_not_download,
                            file_sha256_hashes_match,
                            file_progress,
                            media_info,
                            message_steg)

                with session_scope(DB_PATH) as new_session:
                    message = new_session.query(Messages).filter(
                        Messages.message_id == message_id).first()
                    if message:
                        message.file_progress = "Attachment processing successful"
                        new_session.commit()

        delete_file(download_path)

    return (file_download_successful,
            file_size,
            file_amount,
            file_do_not_download,
            file_sha256_hashes_match,
            file_progress,
            media_info,
            message_steg)


def process_attachments(message_id, extract_path):
    logger.info("{}: Processing attachments in {}".format(
        message_id[-config.ID_LENGTH:].upper(), extract_path))
    media_info = {}
    message_steg = {}
    errors = []

    for dirpath, dirnames, filenames in os.walk(extract_path):
        for f in filenames:
            try:
                fp = os.path.join(dirpath, f)
                logger.info("{}: Processing attachment {}".format(
                    message_id[-config.ID_LENGTH:].upper(), fp))
                if os.path.islink(fp):  # skip symbolic links
                    continue

                file_extension = html.escape(os.path.splitext(f)[1].split(".")[-1].lower())
                attachment_size = os.path.getsize(fp)
                media_height = None
                media_width = None
                steg_msg = None

                if len(file_extension) >= config.MAX_FILE_EXT_LENGTH:
                    errors.append("File extension lengths must be less than {}: {}".format(
                        config.MAX_FILE_EXT_LENGTH, f))

                elif file_extension in config.FILE_EXTENSIONS_IMAGE:
                    logger.info("{}: Attachment is an image".format(
                        message_id[-config.ID_LENGTH:].upper()))
                    # If image file, check for steg message
                    with session_scope(DB_PATH) as new_session:
                        pgp_passphrase_steg = config.PGP_PASSPHRASE_STEG
                        message = new_session.query(Messages).filter(
                            Messages.message_id == message_id).first()
                        if message and message.thread.chan.pgp_passphrase_steg:
                            pgp_passphrase_steg = message.thread.chan.pgp_passphrase_steg

                        logger.info("{}: Checking for steg".format(
                            message_id[-config.ID_LENGTH:].upper()))
                        steg_msg = check_steg(
                            message_id,
                            file_extension,
                            passphrase=pgp_passphrase_steg,
                            file_path=fp)

                        if message:
                            message.file_progress = "Generating image thumbnail"
                            new_session.commit()

                    logger.info("{}: Generating thumbnail".format(
                        message_id[-config.ID_LENGTH:].upper()))
                    thumb_dir = "{}_thumb".format(extract_path)
                    try:
                        os.mkdir(thumb_dir)
                    except:
                        pass
                    img_thumb_filename = "{}/{}".format(thumb_dir, f)
                    logger.info("{}: Generating thumbnail {} with {}".format(
                        message_id[-config.ID_LENGTH:].upper(), img_thumb_filename, fp))
                    generate_thumbnail(
                        message_id, fp, img_thumb_filename, file_extension)

                    try:
                        with session_scope(DB_PATH) as new_session:
                            message = new_session.query(Messages).filter(
                                Messages.message_id == message_id).first()
                            if message:
                                message.file_progress = "Calculating image dimensions"
                                new_session.commit()
                        logger.info("{}: Determining image dimensions".format(message_id[-config.ID_LENGTH:].upper()))
                        Image.MAX_IMAGE_PIXELS = 500000000
                        im = Image.open(fp)
                        media_width, media_height = im.size
                    except UnidentifiedImageError as e:
                        logger.exception("{}: Error identifying image: {}".format(message_id[-config.ID_LENGTH:].upper(), e))
                    except Exception as e:
                        logger.exception("{}: Error opening/stripping image: {}".format(message_id[-config.ID_LENGTH:].upper(), e))

                elif file_extension in config.FILE_EXTENSIONS_VIDEO:
                    try:
                        with session_scope(DB_PATH) as new_session:
                            message = new_session.query(Messages).filter(
                                Messages.message_id == message_id).first()
                            if message:
                                message.file_progress = "Calculating video dimensions"
                                new_session.commit()
                        logger.info("{}: Determining video dimensions".format(message_id[-config.ID_LENGTH:].upper()))
                        vid = cv2.VideoCapture(fp)
                        media_height = vid.get(cv2.CAP_PROP_FRAME_HEIGHT)
                        media_width = vid.get(cv2.CAP_PROP_FRAME_WIDTH)
                        logger.info("{}: Video dimensions: {}x{}".format(
                            message_id[-config.ID_LENGTH:].upper(), media_width, media_height))
                    except Exception as e:
                        logger.exception("{}: Error getting video dimensions: {}".format(message_id[-config.ID_LENGTH:].upper(), e))

                media_info[f] = {}
                if media_height:
                    media_info[f]["height"] = media_height
                if media_width:
                    media_info[f]["width"] = media_width
                media_info[f]["size"] = attachment_size
                media_info[f]["extension"] = file_extension

                if steg_msg:
                    message_steg[f] = steg_msg

            except Exception:
                logger.exception("{}: Error processing file: {}".format(message_id[-config.ID_LENGTH:].upper(), f))

    # Add media info to database
    with session_scope(DB_PATH) as new_session:
        message = new_session.query(Messages).filter(
            Messages.message_id == message_id).first()
        if message:
            message.media_info = json.dumps(media_info)
            message.message_steg = json.dumps(message_steg)
            new_session.commit()

    logger.info("Finished processing attachments")

    return errors, media_info, message_steg


def is_upload_site_in_database(file_upload_settings):
    with session_scope(DB_PATH) as new_session:
        upload_site = new_session.query(UploadSites).filter(and_(
            UploadSites.domain == file_upload_settings["domain"],
            UploadSites.type == file_upload_settings["type"],
            UploadSites.uri == file_upload_settings["uri"],
            UploadSites.download_prefix == file_upload_settings["download_prefix"],
            UploadSites.response == file_upload_settings["response"],
            UploadSites.direct_dl_url == file_upload_settings["direct_dl_url"],
            UploadSites.extra_curl_options == file_upload_settings["extra_curl_options"],
            UploadSites.upload_word == file_upload_settings["upload_word"],
            UploadSites.form_name == file_upload_settings["form_name"],
        )).first()
        if upload_site:
            return True
        else:
            return False
