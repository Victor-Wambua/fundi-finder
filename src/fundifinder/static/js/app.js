// Tiny enhancements. The site works without JavaScript.
document.addEventListener("htmx:configRequest", function (evt) {
  var token = document.body.getAttribute("data-csrf");
  if (token) evt.detail.headers["X-CSRF-Token"] = token;
});
