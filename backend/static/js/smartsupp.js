/* RefundClaimers — Smartsupp live chat loader.
 * Vendor snippet, served from 'self' so it passes the CSP (no inline scripts).
 * The Smartsupp origins it pulls from are allowed in CONTENT_SECURITY_POLICY. */
var _smartsupp = _smartsupp || {};
_smartsupp.key = 'c074c094cbed972fbdb3f86080557acda2e239fa';
window.smartsupp || (function (d) {
  var s, c, o = smartsupp = function () { o._.push(arguments); };
  o._ = [];
  s = d.getElementsByTagName('script')[0];
  c = d.createElement('script');
  c.type = 'text/javascript';
  c.charset = 'utf-8';
  c.async = true;
  c.src = 'https://www.smartsuppchat.com/loader.js?';
  s.parentNode.insertBefore(c, s);
})(document);
