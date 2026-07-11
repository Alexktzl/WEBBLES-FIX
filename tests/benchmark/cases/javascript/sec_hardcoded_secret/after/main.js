const token = process.env.GITHUB_TOKEN;

function auth() {
  return { Authorization: `Bearer ${token}` };
}

module.exports = { auth };
