function requireEnv(name) {
  const value = process.env[name]
  if (!value || !value.trim()) {
    throw new Error(`Required environment variable is missing: ${name}`)
  }
  return value
}

module.exports = { requireEnv }
