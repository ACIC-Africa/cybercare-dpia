/**
 * PrivacyCare (fix wave, item I5): `screening.utils.ts`'s ancestor-prefix
 * walk had no test of its own before this — the closest thing was
 * MappingStepForm.test.tsx's taxonomy mock, which used `tags: []`
 * throughout and so never actually exercised a tagged row, ancestor or
 * otherwise. This is the most drift-prone code in this screen: it mirrors
 * `_TRIGGERING_KEYS_SQL` (src/fides/api/privacycare/special_category.py)
 * by hand on the client, using a copied string constant
 * (`KENYAN_SPECIAL_CATEGORY_TAG`) rather than a shared source of truth, so a
 * change to the backend's tag or walk direction would drift silently unless
 * something here actually pins the current behaviour down.
 */
import { KENYAN_SPECIAL_CATEGORY_TAG } from "./screening.constants";
import {
  anySpecialCategoryKey,
  isBlank,
  isSpecialCategoryKey,
} from "./screening.utils";

// Mirrors the real shape the backend's own loader produces (kenyan.py):
// `user.biometric` is TAGGED (a fideslang default the Kenyan loader tagged
// as a Data Protection Act 2019 §2 special category), while its own
// fideslang descendant `user.biometric.fingerprint` is never re-tagged —
// see this module's own docstring for why a direct lookup on the declared
// key alone would miss it.
const CATEGORIES_BY_KEY = new Map([
  ["user.biometric", { tags: [KENYAN_SPECIAL_CATEGORY_TAG] }],
  ["user.biometric.fingerprint", { tags: [] }],
  [
    "user.demographic.religious_belief",
    { tags: [KENYAN_SPECIAL_CATEGORY_TAG] },
  ],
  ["user.financial", { tags: [] }],
  ["user.financial.income", { tags: [] }],
]);

describe("isSpecialCategoryKey — the ancestor-prefix walk", () => {
  it("is true when the declared key itself carries the tag", () => {
    expect(
      isSpecialCategoryKey(
        "user.demographic.religious_belief",
        CATEGORIES_BY_KEY,
      ),
    ).toBe(true);
  });

  it("is true when an ANCESTOR carries the tag but the declared key is an untagged descendant", () => {
    // The exact scenario this walk exists for: the Kenyan loader tagged
    // user.biometric, not user.biometric.fingerprint — a mapping that
    // declares only the descendant must still flag as special category.
    expect(
      isSpecialCategoryKey("user.biometric.fingerprint", CATEGORIES_BY_KEY),
    ).toBe(true);
  });

  it("is false when neither the key nor any ancestor carries the tag", () => {
    expect(
      isSpecialCategoryKey("user.financial.income", CATEGORIES_BY_KEY),
    ).toBe(false);
  });

  it("is false, not a throw, for a key entirely absent from the map (root-level key, single segment)", () => {
    expect(isSpecialCategoryKey("customer", CATEGORIES_BY_KEY)).toBe(false);
  });

  it("only walks toward the root, never treats a sibling or child as an ancestor", () => {
    // user.financial is untagged; user.financial.income must not somehow
    // pick up a tag from a DIFFERENT top-level branch (user.biometric).
    expect(
      isSpecialCategoryKey("user.financial.income", CATEGORIES_BY_KEY),
    ).toBe(false);
  });
});

describe("anySpecialCategoryKey", () => {
  it("is true when at least one of several declared keys is special, even if the rest are not", () => {
    expect(
      anySpecialCategoryKey(
        ["user.financial.income", "user.biometric.fingerprint"],
        CATEGORIES_BY_KEY,
      ),
    ).toBe(true);
  });

  it("is false when none of the declared keys are special", () => {
    expect(
      anySpecialCategoryKey(
        ["user.financial", "user.financial.income"],
        CATEGORIES_BY_KEY,
      ),
    ).toBe(false);
  });

  it("is false for an empty selection", () => {
    expect(anySpecialCategoryKey([], CATEGORIES_BY_KEY)).toBe(false);
  });
});

describe("isBlank", () => {
  it.each([null, undefined, "", "   ", "\n\t "])("is true for %p", (value) => {
    expect(isBlank(value)).toBe(true);
  });

  it("is false for real, readable content", () => {
    expect(isBlank("No personal data is processed for this activity.")).toBe(
      false,
    );
  });

  it("is false for content padded with whitespace", () => {
    expect(isBlank("  a real reason  ")).toBe(false);
  });
});
