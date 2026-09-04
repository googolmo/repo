(function () {
  function copy(text, button) {
    var done = function () {
      var original = button.textContent;
      button.textContent = "Copied";
      button.classList.add("copied");
      setTimeout(function () {
        button.textContent = original;
        button.classList.remove("copied");
      }, 1400);
    };

    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done).catch(function () {
        fallback(text, done);
      });
    } else {
      fallback(text, done);
    }
  }

  function fallback(text, done) {
    var area = document.createElement("textarea");
    area.value = text;
    document.body.appendChild(area);
    area.select();
    document.execCommand("copy");
    document.body.removeChild(area);
    done();
  }

  document.querySelectorAll("pre[data-copy]").forEach(function (block) {
    var button = document.createElement("button");
    button.type = "button";
    button.className = "copy";
    button.textContent = "Copy";
    button.addEventListener("click", function () {
      var source = block.querySelector("code") || block;
      copy(source.textContent.replace(/\n$/, ""), button);
    });
    block.appendChild(button);
  });
})();
