import { Avatar, Flex, Icons, Paragraph, Text } from "fidesui";

/**
 * PrivacyCare (spec 2026-09-16 D-W2-7g)
 *
 * DESIGN.md, Screen 2: "Prior consultation is a legal obligation with a
 * deadline, not an informational badge. When required, it must read as
 * prominently as the completion summary already does on this page — the
 * same weight, not a quiet row among others... reuse it rather than
 * inventing a third style."
 *
 * The established "message that must not be missed" treatment in this
 * feature is AssessmentCard.tsx's `isComplete` block: an Avatar circle
 * carrying an icon, a bold headline Text, and a secondary Paragraph below
 * it. This banner reuses that exact structure and weight, not a redesign —
 * but recoloured from success/green to error, and CheckmarkFilled swapped
 * for WarningFilled. Reusing the completion treatment's colours verbatim
 * would read as "good news", which a legal filing deadline is not; DESIGN.md
 * asks for the same VISUAL WEIGHT, and weight (an avatar-led two-line
 * callout, not a plain sentence) is what this component reuses. Recolouring
 * for correctness is a judgement call, called out in the build report.
 */
interface PriorConsultationBannerProps {
  /** risk/odpc.py's OdpcFinding.reason — already names the band and the
   * exact deadline wording ("submitted at least N days before this
   * processing activity begins"); rendered verbatim rather than
   * reconstructed here, so a wording or window change in evaluate() never
   * has a second, driftable copy to keep in sync. */
  reason: string;
}

export const PriorConsultationBanner = ({
  reason,
}: PriorConsultationBannerProps) => (
  <Flex
    align="center"
    gap="medium"
    className="rounded-md p-3"
    style={{
      // Same background/border idiom DSRStatusCard.module.scss's own
      // .overdueBadge uses for its "must not be missed" callout — a
      // color-mix tint rather than a guessed *-background/-border token
      // that may not exist as a CSS custom property.
      background:
        "color-mix(in srgb, var(--fidesui-color-error) 10%, transparent)",
      border: "1px solid var(--fidesui-color-error)",
    }}
    data-testid="prior-consultation-required-banner"
  >
    <Avatar
      shape="circle"
      size={28}
      icon={<Icons.WarningFilled size={14} />}
      style={{ backgroundColor: "var(--fidesui-color-error)" }}
    />
    <div>
      <Text strong type="danger" size="sm">
        Prior consultation with the ODPC is required
      </Text>
      <Paragraph type="secondary" size="sm" className="mb-0">
        {reason}
      </Paragraph>
    </div>
  </Flex>
);
