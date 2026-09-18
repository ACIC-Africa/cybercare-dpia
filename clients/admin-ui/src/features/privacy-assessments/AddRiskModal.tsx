import {
  Alert,
  Button,
  Flex,
  Form,
  Input,
  Select,
  Text,
  useMessage,
} from "fidesui";
import { useMemo } from "react";

import { getErrorMessage } from "~/features/common/helpers";
import ConfirmCloseModal from "~/features/common/modals/ConfirmCloseModal";
import { MODAL_SIZE } from "~/features/common/modals/modal-sizes";
import { isAPIError, RTKErrorResult } from "~/types/errors/api";

import {
  LIKELIHOOD_LABELS,
  RISK_BAND_LABELS,
  RISK_CATEGORY_OPTIONS,
  SCALE_OPTIONS,
  SEVERITY_LABELS,
} from "./risk.constants";
import { useAddRiskMutation } from "./risk.slice";
import { RiskBand, RiskCategory } from "./risk.types";
import { bandForScore, bandRank, scoreFor } from "./risk.utils";
import { RiskBandTag } from "./RiskBandTag";

const { Item } = Form;
const { TextArea } = Input;

interface AddRiskFormValues {
  category: RiskCategory;
  description: string;
  likelihood: number;
  severity: number;
}

interface AddRiskModalProps {
  open: boolean;
  onClose: () => void;
  assessmentId: string;
  /** The assessment's CURRENT band (risks[0]?.band ?? low), so the modal
   * can say whether this entry would raise it — DESIGN.md: "when it would
   * raise the assessment's overall band, say so plainly before they
   * commit." */
  currentOverallBand: RiskBand;
}

/**
 * PrivacyCare (spec 2026-09-16 D-W2-7g)
 *
 * Records one risk against a DPIA's register. Dirty-guarded with
 * ConfirmCloseModal — the sibling screening build shipped this OFF for its
 * own mapping step (fix-wave item I2: "a form whose dirty-guard was off, so
 * Escape discarded everything typed with no warning") and this form must
 * not repeat it.
 */
export const AddRiskModal = ({
  open,
  onClose,
  assessmentId,
  currentOverallBand,
}: AddRiskModalProps) => {
  const message = useMessage();
  const [form] = Form.useForm<AddRiskFormValues>();
  const [addRisk, { isLoading: isSaving }] = useAddRiskMutation();

  const likelihood = Form.useWatch("likelihood", form);
  const severity = Form.useWatch("severity", form);

  const preview = useMemo(() => {
    if (!likelihood || !severity) {
      return null;
    }
    const score = scoreFor(likelihood, severity);
    const band = bandForScore(score);
    return {
      score,
      band,
      raisesBand: bandRank(band) > bandRank(currentOverallBand),
    };
  }, [likelihood, severity, currentOverallBand]);

  const handleClose = () => {
    form.resetFields();
    onClose();
  };

  const handleSubmit = async (values: AddRiskFormValues) => {
    try {
      const created = await addRisk({
        assessmentId,
        body: {
          category: values.category,
          description: values.description.trim(),
          likelihood: values.likelihood,
          severity: values.severity,
        },
      }).unwrap();
      message.success(
        `Risk added: ${created.description.slice(0, 60)}${created.description.length > 60 ? "…" : ""}`,
      );
      form.resetFields();
      onClose();
    } catch (error) {
      const typedError = error as RTKErrorResult["error"];
      // Fix wave (Screen 2 review), finding 7 — same reasoning as
      // RemoveRiskModal.tsx's catch block: risk.py's 404 detail text
      // ("no such assessment: 'pa_...'") is written for a log, not a
      // toast. Reachable if the assessment is deleted in another tab while
      // this modal is still open. Raw detail still reaches the console.
      const isNotFound = isAPIError(typedError) && typedError.status === 404;
      if (isNotFound) {
        // eslint-disable-next-line no-console
        console.error("Failed to add risk (404):", typedError);
      }
      message.error(
        isNotFound
          ? "This assessment could not be found. It may have been removed — refresh the page and try again."
          : getErrorMessage(
              typedError,
              "Failed to add the risk. Please try again.",
            ),
      );
    }
  };

  return (
    <ConfirmCloseModal
      title="Add a risk"
      open={open}
      onClose={handleClose}
      getIsDirty={() => form.isFieldsTouched()}
      footer={null}
      width={MODAL_SIZE.md}
      closable={!isSaving}
      maskClosable={!isSaving}
      keyboard={!isSaving}
      destroyOnHidden
    >
      <Form
        form={form}
        layout="vertical"
        onFinish={handleSubmit}
        className="pt-2"
      >
        <Item
          name="category"
          label="Category"
          rules={[{ required: true, message: "Select a category." }]}
        >
          <Select
            aria-label="Category"
            data-testid="input-category"
            placeholder="Select a category"
            options={RISK_CATEGORY_OPTIONS}
          />
        </Item>

        <Item
          name="description"
          label="Description"
          required
          rules={[
            {
              validator: async (_rule, value: string) => {
                if (!value || value.trim().length === 0) {
                  throw new Error("Describe what this risk is.");
                }
              },
            },
          ]}
        >
          <TextArea
            aria-label="Description"
            data-testid="input-description"
            rows={3}
            placeholder="e.g. Fuel card PINs are logged in plaintext to the ops console"
          />
        </Item>

        <Flex gap="medium">
          <Item
            name="likelihood"
            label="Likelihood"
            className="flex-1"
            rules={[{ required: true, message: "Select a likelihood." }]}
          >
            <Select
              aria-label="Likelihood"
              data-testid="input-likelihood"
              placeholder="Select likelihood"
              options={SCALE_OPTIONS(LIKELIHOOD_LABELS)}
            />
          </Item>

          <Item
            name="severity"
            label="Severity"
            className="flex-1"
            rules={[{ required: true, message: "Select a severity." }]}
          >
            <Select
              aria-label="Severity"
              data-testid="input-severity"
              placeholder="Select severity"
              options={SCALE_OPTIONS(SEVERITY_LABELS)}
            />
          </Item>
        </Flex>

        {/* DESIGN.md: "show the consequence while they choose" — the
            resulting score and band, live, and a plain statement when this
            entry would raise the assessment's overall band. */}
        {preview && (
          <Alert
            type={preview.raisesBand ? "warning" : "info"}
            showIcon
            className="mb-4"
            data-testid="add-risk-consequence"
            message={
              <Flex align="center" gap="small">
                <Text>Score {preview.score} of 25 —</Text>
                <RiskBandTag band={preview.band} />
              </Flex>
            }
            description={
              preview.raisesBand
                ? `This risk would raise the assessment to ${RISK_BAND_LABELS[preview.band]}.`
                : undefined
            }
          />
        )}

        <Flex justify="end" gap="small">
          <Button onClick={handleClose} disabled={isSaving}>
            Cancel
          </Button>
          <Button
            type="primary"
            htmlType="submit"
            loading={isSaving}
            data-testid="submit-add-risk"
          >
            Add risk
          </Button>
        </Flex>
      </Form>
    </ConfirmCloseModal>
  );
};
