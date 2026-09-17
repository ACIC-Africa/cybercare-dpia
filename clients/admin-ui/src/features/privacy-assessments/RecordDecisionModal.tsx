import {
  Alert,
  Button,
  Checkbox,
  Flex,
  Form,
  Input,
  Result,
  Space,
  Spin,
  Text,
  useMessage,
} from "fidesui";
import { useMemo, useState } from "react";

import { getErrorMessage } from "~/features/common/helpers";
import ConfirmCloseModal from "~/features/common/modals/ConfirmCloseModal";
import { MODAL_SIZE } from "~/features/common/modals/modal-sizes";
import { RTKErrorResult } from "~/types/errors/api";

import { MappingStepForm } from "./MappingStepForm";
import {
  useGetScreeningTriggersQuery,
  useRecordScreeningDecisionMutation,
} from "./screening.slice";
import { isBlank } from "./screening.utils";

const { Item } = Form;
const { TextArea } = Input;

interface DecisionFormValues {
  triggeredKeys: string[];
  justification: string;
}

export type RecordDecisionStep = "decision" | "mapping";

interface RecordDecisionModalProps {
  open: boolean;
  onClose: () => void;
  businessProcessId: string;
  processName: string;
  /** From the screening list's own `has_mapping` for this row — passed
   * through to the mapping step so it can tell "never mapped through this
   * route" apart from "mapped, but by an activity this route does not
   * own" when the mapping read itself comes back null. */
  hasMapping: boolean;
  /** "mapping" opens straight into the mapping step — used by the table's
   * own "Start/complete mapping" action on an already-applicable row,
   * skipping a re-screen the officer did not ask for. */
  initialStep?: RecordDecisionStep;
}

/**
 * PrivacyCare (spec 2026-09-16 D-W2-7g)
 *
 * The two-step "record a decision, then capture the mapping" flow DESIGN.md
 * describes for Screen 1. Step 1 records a screening decision; marking a
 * process applicable opens Step 2 in the same modal, because a process that
 * needs an assessment cannot have one until somebody says what data it
 * touches — capturing that here, at the moment the decision is made, is the
 * whole reason this screen exists.
 */
export const RecordDecisionModal = ({
  open,
  onClose,
  businessProcessId,
  processName,
  hasMapping,
  initialStep = "decision",
}: RecordDecisionModalProps) => {
  const message = useMessage();
  const [form] = Form.useForm<DecisionFormValues>();
  const [step, setStep] = useState<RecordDecisionStep>(initialStep);
  const [isMappingSaving, setIsMappingSaving] = useState(false);
  // I2: the mapping step keeps its own Form instance (MappingStepForm owns
  // it), so this modal cannot read its dirty state the way it reads the
  // decision step's own `form.isFieldsTouched()` below. MappingStepForm
  // reports it here instead — see its own onDirtyChange prop.
  const [isMappingDirty, setIsMappingDirty] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  const {
    data: triggersData,
    isLoading: isLoadingTriggers,
    isError: isTriggersError,
    refetch: refetchTriggers,
  } = useGetScreeningTriggersQuery();

  const [recordDecision, { isLoading: isRecording }] =
    useRecordScreeningDecisionMutation();

  const triggers = useMemo(
    () =>
      [...(triggersData?.triggers ?? [])].sort(
        (a, b) => a.display_order - b.display_order,
      ),
    [triggersData],
  );

  const reset = () => {
    form.resetFields();
    setStep(initialStep);
    setSubmitError(null);
    setIsMappingDirty(false);
  };

  const handleClose = () => {
    reset();
    onClose();
  };

  const handleDecisionSubmit = async (values: DecisionFormValues) => {
    setSubmitError(null);
    const isApplicable = values.triggeredKeys.length > 0;
    try {
      await recordDecision({
        businessProcessId,
        body: {
          triggered_keys: values.triggeredKeys,
          justification: isApplicable ? null : values.justification,
        },
      }).unwrap();
      message.success(
        `${processName} marked ${isApplicable ? "applicable" : "not applicable"}.`,
      );
      if (isApplicable) {
        setStep("mapping");
      } else {
        handleClose();
      }
    } catch (error) {
      setSubmitError(
        getErrorMessage(
          error as RTKErrorResult["error"],
          "Failed to record the decision. Please try again.",
        ),
      );
    }
  };

  const isSaving = isRecording || isMappingSaving;

  return (
    <ConfirmCloseModal
      title={
        step === "decision"
          ? "Record a screening decision"
          : "Capture the data mapping"
      }
      open={open}
      onClose={handleClose}
      // I2: was `step === "decision" && form.isFieldsTouched()` — the
      // mapping step's own dirty guard was always off, so Escape or an
      // overlay click silently discarded a six-field form with no warning,
      // exactly the form DESIGN.md itself calls out as the one that gets
      // abandoned if it demands too much before saving anything. Both
      // steps are now guarded, each from its own signal.
      getIsDirty={() =>
        step === "decision" ? form.isFieldsTouched() : isMappingDirty
      }
      footer={null}
      width={MODAL_SIZE.md}
      closable={!isSaving}
      maskClosable={!isSaving}
      keyboard={!isSaving}
      destroyOnHidden
    >
      <Space orientation="vertical" size="large" className="w-full pt-2">
        <Text strong>{processName}</Text>

        {step === "decision" && (
          <>
            {isLoadingTriggers && (
              <Flex justify="center" className="py-8">
                <Spin />
              </Flex>
            )}

            {isTriggersError && (
              <Result
                status="error"
                title="Failed to load the screening questions."
                subTitle="Please try again."
                extra={
                  <Button type="primary" onClick={() => refetchTriggers()}>
                    Retry
                  </Button>
                }
              />
            )}

            {!isLoadingTriggers && !isTriggersError && (
              <Form
                form={form}
                layout="vertical"
                onFinish={handleDecisionSubmit}
                initialValues={{ triggeredKeys: [], justification: "" }}
                onValuesChange={() => setSubmitError(null)}
              >
                <Item name="triggeredKeys" valuePropName="value">
                  <Checkbox.Group className="w-full">
                    <Space
                      orientation="vertical"
                      size="middle"
                      className="w-full"
                    >
                      {triggers.map((trigger) => (
                        <Checkbox
                          key={trigger.id}
                          value={trigger.trigger_key}
                          data-testid={`trigger-${trigger.trigger_key}`}
                        >
                          <Text>{trigger.label}</Text>
                          <Text type="secondary" size="sm" className="block">
                            {trigger.description}
                          </Text>
                        </Checkbox>
                      ))}
                    </Space>
                  </Checkbox.Group>
                </Item>

                {/* The rule, made visible — DESIGN.md: state the
                    consequence live, above the buttons, so the user is
                    never guessing what their ticks mean. */}
                <Item
                  noStyle
                  shouldUpdate={(prev, curr) =>
                    prev.triggeredKeys !== curr.triggeredKeys
                  }
                >
                  {({ getFieldValue }) => {
                    const triggeredKeys: string[] =
                      getFieldValue("triggeredKeys") ?? [];
                    const isApplicable = triggeredKeys.length > 0;
                    return (
                      <Alert
                        type={isApplicable ? "success" : "info"}
                        showIcon
                        message={
                          isApplicable
                            ? "This process is applicable and needs an assessment."
                            : "No questions apply. This process is not applicable and will not receive an assessment. A reason is required."
                        }
                        data-testid="decision-consequence"
                      />
                    );
                  }}
                </Item>

                <Item
                  noStyle
                  shouldUpdate={(prev, curr) =>
                    prev.triggeredKeys !== curr.triggeredKeys
                  }
                >
                  {({ getFieldValue }) => {
                    const triggeredKeys: string[] =
                      getFieldValue("triggeredKeys") ?? [];
                    if (triggeredKeys.length > 0) {
                      return null;
                    }
                    return (
                      <Item
                        name="justification"
                        label="Reason"
                        // I6: `required` is set explicitly because the only
                        // rule here is a custom validator, not `{ required:
                        // true }` — AntD only auto-renders the asterisk for
                        // the latter, so without this the field silently had
                        // no required marker at all; nothing but the
                        // consequence alert above explained why the submit
                        // button stayed disabled.
                        required
                        rules={[
                          {
                            validator: async (_rule, value: string) => {
                              if (isBlank(value)) {
                                throw new Error(
                                  "Give a reason. This is the record that explains why this business process has no assessment.",
                                );
                              }
                            },
                          },
                        ]}
                      >
                        <TextArea
                          aria-label="Reason"
                          data-testid="input-justification"
                          rows={3}
                          placeholder="Why does this process not need a DPIA?"
                        />
                      </Item>
                    );
                  }}
                </Item>

                {/* DESIGN.md: put this above the submit button, never
                    after it — a screening decision is permanent. */}
                <Alert
                  type="warning"
                  showIcon
                  className="mb-4"
                  message="A screening decision is a permanent record."
                  description="It cannot be edited or deleted. Screening this process again adds a new decision and leaves this one in place."
                  data-testid="permanence-caution"
                />

                {submitError && (
                  <Alert
                    type="error"
                    showIcon
                    className="mb-4"
                    message="Could not record the decision"
                    description={submitError}
                  />
                )}

                <Item noStyle shouldUpdate>
                  {({ getFieldValue, getFieldsError }) => {
                    const triggeredKeys: string[] =
                      getFieldValue("triggeredKeys") ?? [];
                    const justification: string =
                      getFieldValue("justification") ?? "";
                    const blockedByBlankReason =
                      triggeredKeys.length === 0 && isBlank(justification);
                    const hasFieldErrors = getFieldsError().some(
                      (f) => f.errors.length > 0,
                    );
                    return (
                      <Flex justify="end" gap="small">
                        <Button onClick={handleClose} disabled={isSaving}>
                          Cancel
                        </Button>
                        <Button
                          type="primary"
                          htmlType="submit"
                          loading={isRecording}
                          disabled={blockedByBlankReason || hasFieldErrors}
                        >
                          Record decision
                        </Button>
                      </Flex>
                    );
                  }}
                </Item>
              </Form>
            )}
          </>
        )}

        {step === "mapping" && (
          <MappingStepForm
            businessProcessId={businessProcessId}
            processName={processName}
            hasMappingFromList={hasMapping}
            onSaved={handleClose}
            onCancel={handleClose}
            onSavingChange={setIsMappingSaving}
            onDirtyChange={setIsMappingDirty}
          />
        )}
      </Space>
    </ConfirmCloseModal>
  );
};
