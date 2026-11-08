function isValidName(name) {
  return typeof name === 'string' && name.trim().length > 0;
}

function logError(err) {
  console.error(`error: ${err.message}`);
}

module.exports = { isValidName, logError };
