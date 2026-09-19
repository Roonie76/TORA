// Spendsy's own @shared/utils/cn, reproduced for the tests in this repo.
//
// The TORA components import "@shared/utils/cn", which resolves inside a Spendsy checkout and
// nowhere else. That dependency is real and intentional — these components live in Spendsy — but
// it meant the tests could not run from this repo at all. This stub is the same two lines as
// Spendsy's, so the tests exercise the same class merging the app does.
import { clsx } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs) {
  return twMerge(clsx(inputs));
}

export default cn;
