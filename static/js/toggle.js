expanded_files = {};

$(function() {
    $('.pic').click(function() {
        id = $(this).attr('id');
        name = $(this).attr('name');
        let type = document.getElementById("type_" + id).value;
        let filename = document.getElementById("filename_" + id + "_" + name).value;
        let img_width = document.getElementById("width_" + id + "_" + name).value;
        let img_height = document.getElementById("height_" + id + "_" + name).value;
        let src_thumb = document.getElementById("src_thumb_" + id + "_" + name).value;
        let spoiler = document.getElementById("spoiler_" + id + "_" + name).value;
        let newline = document.getElementById("newline_" + id);
        let num_files = parseInt(document.getElementById("num_files_" + id).value);
        let truncate = document.getElementById("truncate_" + id).value;
        let src_full = "/files/image/" + id + "/" + filename;

        if ($(this).attr('src') == src_full) {
            $(this).attr("src", "");
            $(this).attr("src", src_thumb);
            if (type === "op") {
                new_height = "200px";
                new_width = "200px";
            }
            else if (type === "reply") {
                new_height = "130px";
                new_width = "130px";
            }
            expanded_files[id] = expanded_files[id].filter(e => e !== id + "_" + name);
            if (expanded_files[id].length == 0 && num_files < 3 && truncate === "0") newline.style.display = "none";
            $(this).animate({'max-width': new_width}, 0);
            $(this).animate({'max-height': new_height}, 0);
        }
        else if ($(this).attr('src') == src_thumb) {
            $(this).attr("src", "");
            $(this).attr("src", src_full);
            if (img_width > window.innerWidth) {
                new_width = "98%";
                new_height = "98%";
            }
            else {
                new_width = img_width;
                new_height = img_height;
            }
            if (!(id in expanded_files)) expanded_files[id] = [];
            expanded_files[id].push(id + "_" + name);
            newline.style.display = "block";
            $(this).animate({'max-width': new_width}, 0);
            $(this).animate({'max-height': new_height}, 0);
        }
    });
});

$(function() {
    $('.video').click(function() {
        id = $(this).attr('id');
        name = $(this).attr('name');
        let type = document.getElementById("type_" + id).value;
        current_height = $(this).css('height');
        let width = document.getElementById("width_" + id + "_" + name).value;
        let height = document.getElementById("height_" + id + "_" + name).value;
        let num_files = parseInt(document.getElementById("num_files_" + id).value);
        let truncate = document.getElementById("truncate_" + id).value;
        let newline = document.getElementById("newline_" + id);
        if (type === "op") {
            thumb_width = "275px";
            thumb_height = "200px";
        }
        else if (type === "reply") {
            thumb_width = "190px";
            thumb_height = "130px";
        }
        if (current_height === thumb_height) {
            if (width > window.innerWidth) {
                new_width = "98%";
                new_height = "98%";
            }
            else {
                new_width = width + "px";
                new_height = (parseInt(height) + 20) + "px";
            }
            if (!(id in expanded_files)) expanded_files[id] = [];
            expanded_files[id].push(id + "_" + name);
            newline.style.display = "block";
            $(this).animate({width: new_width}, 0);
            $(this).animate({height: new_height}, 0);
        } else {
            expanded_files[id] = expanded_files[id].filter(e => e !== id + "_" + name);
            if (expanded_files[id].length == 0 && num_files < 3 && truncate === "0") newline.style.display = "none";
            $(this).animate({width: thumb_width}, 0);
            $(this).animate({height: thumb_height}, 0);
        }
    });
});
