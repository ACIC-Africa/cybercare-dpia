import { Alert, Button, Flex, Modal, Text, useMessage } from "fidesui";
import { useMemo } from "react";

import { getErrorMessage } from "~/features/common/helpers";
import { MODAL_SIZE } from "~/features/common/modals/modal-sizes";
import { isAPIError, RTKErrorResult } from "~/types/errors/api";

import { RISK_BAND_LABELS, RISK_CATEGORY_LABELS } from "./risk.constants";
import { useRemoveRiskMutation } from "./risk.slice";
import { RiskCategory, RiskResponse } from "./risk.types";
import { overallBandForScores } from "./risk.utils";

interface RemoveRiskModalProps {
  open: boolean;
  onClose: () => void;
  assessmentId: string;
  risk: RiskResponse;
  /** Every OTHER risk currently on the register, used to preview what the
   * band becomes once this one is gone — DESIGN.md: "when it would change
   * the band, says what it becomes." */
  remainingRisks: RiskResponse[];
}

/**
 * PrivacyCare (spec 2026-09-16 D-W2-7g)
 *
 * Confirms a risk's removal, naming it, and previewing the resulting band
 * when removal would change it. Removal is not a form with fields to lose,
 * so this uses a plain Modal (not ConfirmCloseModal) — Escape/Cancel here
 * always discards nothing, because nothing was entered.
 */
export const RemoveRiskModal = ({
  open,
  onClose,
  assessmentId,
  risk,
  remainingRisks,
}: RemoveRiskModalProps) => {
  const message = useMessage();
  const [removeRisk, { isLoading: isRemoving }] = useRemoveRiskMutation();

  const currentBand = overallBandForScores(
    [risk, ...remainingRisks].map((r) => r.score),
  );
  const bandAfterRemoval = useMemo(
    () => overallBandForScores(remainingRisks.map((r) => r.score)),
    [remainingRisks],
  );
  const bandWouldChange = bandAfterRemoval !== currentBand;

  const handleRemove = async () => {
    try {
      await removeRisk({ riskId: risk.id, assessmentId }).unwrap();
      message.success(
        `Removed: ${risk.description.slice(0, 60)}${risk.description.length > 60 ? "…" : ""}`,
      );
      onClose();
    } catch (error) {
      const typedError = error as RTKErrorResult["error"];
      // Fix wave (Screen 2 review), finding 7: getErrorMessage forwards a
      // string `detail` verbatim, and risk.py's 404s are written for a
      // developer reading a log ("no such risk: 'risk_xxx'"), not for a
      // privacy officer reading a toast. Reachable if the risk (or its
      // assessment) is deleted in another tab between opening this modal
      // and confirming removal. The raw detail still reaches the console
      // for debugging; the toast gets a sentence written for her.
      const isNotFound = isAPIError(typedError) && typedError.status === 404;
      if (isNotFound) {
        // eslint-disable-next-line no-console
        console.error("Failed to remove risk (404):", typedError);
      }
      message.error(
        isNotFound
          ? "This risk could not be found. It may already have been removed — refresh the page and try again."
          : getErrorMessage(
              typedError,
              "Failed to remove the risk. Please try again.",
            ),
      );
    }
  };

  return (
    <Modal
      title="Remove this risk?"
      open={open}
      onCancel={onClose}
      footer={null}
      width={MODAL_SIZE.md}
      closable={!isRemoving}
      maskClosable={!isRemoving}
      keyboard={!isRemoving}
      destroyOnHidden
    >
      <Flex vertical gap="middle" className="pt-2">
        <div>
          <Text strong>
            {RISK_CATEGORY_LABELS[risk.category as RiskCategory] ??
              risk.category}
          </Text>
          <Text className="block" size="sm">
            {risk.description}
          </Text>
        </div>

        {/* DESIGN.md: this caution appears before the destructive action,
            not after it — same placement discipline the screening screen's
            own permanence caution uses. */}
        <Alert
          type="warning"
          showIcon
          // Fix wave (Screen 2 review), finding 4: DESIGN.md's language
          // table requires "Office of the Data Protection Commissioner
          // (ODPC)" spelled out on first use, then ODPC — this modal used
          // "ODPC" with no expansion anywhere in it.
          message="Removing a risk recalculates the assessment's risk band immediately, and may change whether prior consultation with the Office of the Data Protection Commissioner (ODPC) is required."
          description={
            <>
              {bandWouldChange && (
                <Text className="block" size="sm">
                  {/* Fix wave (Screen 2 review), finding 3: this used to
                      render the raw wire values ("critical" -> "low"). Every
                      other place a band appears on screen — including the
                      Add modal's own twin sentence — goes through
                      RISK_BAND_LABELS ("Critical" -> "Low"); this is the one
                      place that had drifted from that rule. */}
                  The risk band will change from{" "}
                  {RISK_BAND_LABELS[currentBand]} to{" "}
                  {RISK_BAND_LABELS[bandAfterRemoval]}.
                </Text>
              )}
              <Text className="block" size="sm">
                Removal cannot be undone.
              </Text>
            </>
          }
          data-testid="remove-risk-caution"
        />

        <Flex justify="end" gap="small">
          <Button onClick={onClose} disabled={isRemoving}>
            Cancel
          </Button>
          <Button
            danger
            type="primary"
            loading={isRemoving}
            onClick={handleRemove}
            data-testid="confirm-remove-risk"
          >
            Remove risk
          </Button>
        </Flex>
      </Flex>
    </Modal>
  );
};
