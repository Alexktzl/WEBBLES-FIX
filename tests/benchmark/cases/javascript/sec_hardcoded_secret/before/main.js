const token = "ghp_AbC123dEf456GhI789";

function auth() {
  return { Authorization: `Bearer ${token}` };
}

module.exports = { auth };
