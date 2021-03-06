define(function() {

    "use strict";

    // http://chuvash.eu/2011/12/14/the-cleanest-auto-resize-for-a-textarea/
    var autoresize = function(textarea, minheight) {
        var offset= !window.opera ? (textarea.offsetHeight - textarea.clientHeight) : (textarea.offsetHeight + parseInt(window.getComputedStyle(textarea, null).getPropertyValue('border-top-width')));
        ["keyup", "focus"].forEach(function(event) {
            textarea.on(event, function() {
                if ((textarea.scrollHeight  + offset ) > minheight) {
                    textarea.style.height = "auto";
                    textarea.style.height = (textarea.scrollHeight  + offset ) + 'px';
                }
            });
        });
    };

    return {
        autoresize: autoresize
    };
});
