import { Alert, Button, Flex, Modal, Text, useMessage } from "fidesui";
import { useMemo } from "react";

import { getErrorMessage } from "~/features/common/helpers";
import { MODAL_SIZE } from "~/features/common/modals/modal-sizes";
import { RTKErrorResult } from "~/types/errors/api";

import { RISK_CATEGORY_LABELS } from "./risk.constants";
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
      message.error(
        getErrorMessage(
          error as RTKErrorResult["error"],
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
          message="Removing a risk recalculates the assessment's risk band immediately, and may change whether prior consultation with the ODPC is required."
          description={
            <>
              {bandWouldChange && (
                <Text className="block" size="sm">
                  The risk band will change from {currentBand} to{" "}
                  {bandAfterRemoval}.
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
