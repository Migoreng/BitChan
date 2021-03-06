import logging
import signal
import sys

from flask import Flask

import config
from database.models import populate_db
from flask_extensions import db

logger = logging.getLogger('bitchan')

app = Flask(__name__)

app.config.from_object(config.ProdConfig)
app.jinja_env.add_extension('jinja2.ext.do')
db.init_app(app)

with app.app_context():
    db.create_all()
    populate_db()

from bitchan import bitchan as nexus

from flask_routes import routes_address_book
from flask_routes import routes_admin
from flask_routes import routes_board
from flask_routes import routes_identities
from flask_routes import routes_list
from flask_routes import routes_mail
from flask_routes import routes_main
from flask_routes import routes_management
from flask_routes import routes_pgp

app.register_blueprint(routes_address_book.blueprint)
app.register_blueprint(routes_admin.blueprint)
app.register_blueprint(routes_board.blueprint)
app.register_blueprint(routes_identities.blueprint)
app.register_blueprint(routes_list.blueprint)
app.register_blueprint(routes_mail.blueprint)
app.register_blueprint(routes_main.blueprint)
app.register_blueprint(routes_management.blueprint)
app.register_blueprint(routes_pgp.blueprint)


def signal_handler(signal, frame):
    sys.exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal_handler)
    app.run(port=8080, debug=True)
