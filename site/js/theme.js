// Before first paint, so a light-theme reader never sees a dark flash.
if (localStorage.getItem('perf-eval-theme') === 'light') {
  document.documentElement.setAttribute('data-theme', 'light');
}
