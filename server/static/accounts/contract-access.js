// Load this employee's saved settings before the leader changes them.
const employee = document.querySelector('#id_employee');
if (employee) employee.addEventListener('change', () => {
  if (employee.value) window.location.assign(window.location.pathname + '?employee=' + encodeURIComponent(employee.value));
});
