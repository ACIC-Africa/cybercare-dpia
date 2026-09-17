/**
 * PrivacyCare (spec 2026-09-16 D-W2-7g)
 *
 * Non-component helpers for the DPIA screening & mapping screen.
 */
import { DataCategory } from "~/types/api";

import { KENYAN_SPECIAL_CATEGORY_TAG } from "./screening.constants";

/**
 * Whether `dataCategoryKey` (or any ancestor of it, by dotted-path prefix)
 * carries the Kenyan special-category tag.
 *
 * Mirrors the backend's own ancestor-prefix walk
 * (`_TRIGGERING_KEYS_SQL` in `src/fides/api/privacycare/special_category.py`)
 * client-side, for a live notice as the officer picks categories — the
 * server remains the source of truth
 * (`DataMappingResponse.processes_special_category_data`, returned after
 * save). A direct tag lookup on the declared key alone is NOT enough: the
 * Kenyan taxonomy loader tags the ANCESTOR category it added (e.g.
 * `user.biometric`), and fideslang's own pre-existing descendant rows
 * (e.g. `user.biometric.fingerprint`) never get re-tagged — a mapping that
 * declares only the descendant would otherwise miss the flag entirely.
 */
export const isSpecialCategoryKey = (
  dataCategoryKey: string,
  categoriesByKey: ReadonlyMap<string, Pick<DataCategory, "tags">>,
): boolean => {
  const parts = dataCategoryKey.split(".");
  for (let depth = 1; depth <= parts.length; depth += 1) {
    const ancestorKey = parts.slice(0, depth).join(".");
    if (
      categoriesByKey
        .get(ancestorKey)
        ?.tags?.includes(KENYAN_SPECIAL_CATEGORY_TAG)
    ) {
      return true;
    }
  }
  return false;
};

export const anySpecialCategoryKey = (
  dataCategoryKeys: string[],
  categoriesByKey: ReadonlyMap<string, Pick<DataCategory, "tags">>,
): boolean =>
  dataCategoryKeys.some((key) => isSpecialCategoryKey(key, categoriesByKey));

/** Whitespace-only counts as blank — the whole point of the reason field is
 * a real, readable justification. */
export const isBlank = (value: string | null | undefined): boolean =>
  !value || value.trim().length === 0;
