/* Shared by every page: the saved colour scheme, the theme switch, and a "themechange" event for charts. */
(function () {
  'use strict';
  var root = document.documentElement;
  try { var saved = localStorage.getItem('pf-theme'); if (saved === 'light' || saved === 'dark') root.setAttribute('data-theme', saved); } catch (e) {}
  function eff() { var t = root.getAttribute('data-theme'); return t || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'); }
  function announce() { document.dispatchEvent(new CustomEvent('themechange', { detail: eff() })); }
  function label(btn) { btn.textContent = eff() === 'dark' ? 'Light' : 'Dark'; btn.setAttribute('aria-label', 'Switch to the ' + (eff() === 'dark' ? 'light' : 'dark') + ' colour scheme'); }
  document.addEventListener('DOMContentLoaded', function () {
    var btns = document.querySelectorAll('.theme-btn');
    btns.forEach(function (b) {
      label(b);
      b.addEventListener('click', function () {
        var t = eff() === 'dark' ? 'light' : 'dark';
        root.setAttribute('data-theme', t);
        try { localStorage.setItem('pf-theme', t); } catch (e) {}
        btns.forEach(label); announce();
      });
    });
    matchMedia('(prefers-color-scheme: dark)').addEventListener('change', function () { btns.forEach(label); announce(); });
  });
})();
