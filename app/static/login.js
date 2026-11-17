// Kept out of login.html so the Content-Security-Policy can forbid inline scripts.
const q = new URLSearchParams(location.search);
document.getElementById('next').value = q.get('next') || '/';
const err = q.get('error');
if (err) {
  const box = document.getElementById('error');
  box.textContent = err === 'locked'
    ? 'Too many failed attempts. Try again in 15 minutes.'
    : 'Wrong password.';
  box.hidden = false;
}
