/* Masakhane site-wide page width enhancer - Ver. 0.0.0.1.
   Finds a narrow centered page shell around the primary heading; never touches the sidebar. */
(function () {
  'use strict';
  function applyWideLayout() {
    document.querySelectorAll('.masakhane-wide-content').forEach(function (node) {
      node.classList.remove('masakhane-wide-content');
    });
    if (window.innerWidth < 1024) return;
    var main = document.querySelector('main') ||
      document.querySelector('[role="main"]') ||
      document.querySelector('#main-content') ||
      document.querySelector('.main-content') ||
      document.querySelector('.content-area');
    var headings = Array.from((main || document).querySelectorAll('h1, h2'))
      .filter(function (node) {
        return !node.closest('header, nav, aside, .sidebar, .topbar, .navbar');
      });
    if (!headings.length) return;
    var heading = headings[0];
    var match = null;
    var node = heading.parentElement;
    while (node && node !== document.body) {
      var parent = node.parentElement;
      if (!parent) break;
      var bounds = node.getBoundingClientRect();
      var outer = parent.getBoundingClientRect();
      if (bounds.width >= 550 && bounds.width <= 1350 &&
          bounds.height >= 240 && outer.width >= bounds.width + 110 &&
          outer.width >= 900 && !node.matches('table, form, aside, nav, header')) {
        match = node;
        break;
      }
      node = parent;
    }
    if (match) match.classList.add('masakhane-wide-content');
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', applyWideLayout);
  } else {
    applyWideLayout();
  }
  window.addEventListener('resize', function () {
    window.requestAnimationFrame(applyWideLayout);
  }, {passive: true});
  window.addEventListener('pageshow', applyWideLayout);
})();
