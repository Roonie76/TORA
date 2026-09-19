// Stand-in for Spendsy's own src/api.js, for this repo's tests only.
//
// It sits at src/api.js because that is where Spendsy keeps it: TORAPage imports
// "../api" and the tests mock "../../api", and both have to resolve to the same module
// for the mock to take. scripts/install-into-spendsy.mjs does NOT copy this file — the
// real one belongs to Spendsy.
//
// TORAPage imports { getStoredAccessToken } from "../api" — Spendsy's module, which holds the
// signed-in user's token so TORA can read their own records. Only the one function is needed
// here; the tests that care about auth headers set the token themselves.
export function getStoredAccessToken() {
  try {
    return localStorage.getItem("spendsy_access_token");
  } catch {
    return null;
  }
}

export default { getStoredAccessToken };
