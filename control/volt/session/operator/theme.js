/* Sets the chosen theme before first paint so there is no flash. Auto (no choice) follows the operating system. */
'use strict';
try {
  var t = localStorage.getItem('volt.theme');
  if (t === 'dark' || t === 'light') document.documentElement.setAttribute('data-theme', t);
} catch (e) { /* storage blocked: stay on Auto */ }
