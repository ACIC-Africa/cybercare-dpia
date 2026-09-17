import { Avatar, Icons, Result } from "fidesui";

/**
 * PrivacyCare (spec 2026-09-16 D-W2-7g): shown only when
 * `GET /api/v1/privacycare/screening` returns zero business processes —
 * i.e. the business process register itself is empty, not merely that no
 * processing activity has been mapped yet. Business processes, not
 * processing activities, are what this list is keyed to (plan 20, Task 3),
 * so this differs from the sibling `EmptyState` in this same directory,
 * which is about assessments having never been generated.
 *
 * No call-to-action button here, deliberately: there is no admin UI screen
 * yet for creating or importing a business process (they are loaded
 * directly into `privacycare_business_process`) — a button pointing
 * somewhere would be a dead end, and this screen's own report names that
 * gap for Product rather than papering over it with a link to somewhere
 * unrelated.
 */
export const ScreeningEmptyState = () => (
  <Result
    icon={
      <Avatar
        shape="square"
        variant="outlined"
        size={64}
        icon={<Icons.Document size={32} />}
      />
    }
    title="No business processes to screen"
    subTitle="Business processes come from the business process register, loaded separately from this screen. There is nothing to screen until at least one exists."
    className="mt-20"
  />
);
