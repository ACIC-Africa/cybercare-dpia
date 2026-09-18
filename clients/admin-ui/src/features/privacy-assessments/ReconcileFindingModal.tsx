import {
  Alert,
  Button,
  Flex,
  Form,
  Input,
  Radio,
  Select,
  Space,
  Text,
  useMessage,
} from "fidesui";
import { useState } from "react";

import { getErrorMessage } from "~/features/common/helpers";
import ConfirmCloseModal from "~/features/common/modals/ConfirmCloseModal";
import { MODAL_SIZE } from "~/features/common/modals/modal-sizes";
import { RTKErrorResult } from "~/types/errors/api";

import { useReconcileDiscoveryFindingMutation } from "./discovery-findings.slice";
import { FindingResponse } from "./discovery-findings.types";
import { useSystemCandidatesFromFindings } from "./discoverySystemCandidates";
import { isBlank } from "./screening.utils";

const { Item } = Form;
const { TextArea } = Input;

type ReconcileChoice = "mapped" | "ignored";

interface FormValues {
  choice: ReconcileChoice;
  systemId: string | undefined;
  reason: string;
}

interface ReconcileFindingModalProps {
  open: boolean;
  onClose: () => void;
  finding: FindingResponse;
  /** Every finding currently loaded on the table, so the "mark as mapped"
   * picker can be built from the systems discovery already knows about —
   * see discoverySystemCandidates.ts for why this is the only legitimate
   * source. */
  allFindings: FindingResponse[];
}

/**
 * PrivacyCare — Discovery, Screen 4 of docs/design/privacycare-screens/DESIGN.md.
 *
 * "Reconciling a finding is the write action: mark it as belonging to a
 * system, or ignore it with a written reason. Ignoring without a reason is
 * how a register quietly loses something — require the reason, the same
 * way a not-applicable screening decision requires one." Same permanence
 * caution, same required-reason discipline as RecordDecisionModal — a
 * reconciliation is a record of somebody having looked, not a toggle.
 */
export const ReconcileFindingModal = ({
  open,
  onClose,
  finding,
  allFindings,
}: ReconcileFindingModalProps) => {
  const message = useMessage();
  const [form] = Form.useForm<FormValues>();
  const [submitError, setSubmitError] = useState<string | null>(null);

  const systemCandidates = useSystemCandidatesFromFindings(allFindings);

  const [reconcile, { isLoading: isSaving }] =
    useReconcileDiscoveryFindingMutation();

  const tableLabel = `${finding.schema_name}.${finding.table_name}`;

  const handleClose = () => {
    form.resetFields();
    setSubmitError(null);
    onClose();
  };

  const handleSubmit = async (values: FormValues) => {
    setSubmitError(null);
    try {
      await reconcile({
        urn: finding.urn,
        body:
          values.choice === "mapped"
            ? { state: "mapped", system_id: values.systemId }
            : { state: "ignored", reason: values.reason },
      }).unwrap();
      message.success(
        values.choice === "mapped"
          ? `${tableLabel} marked as already mapped.`
          : `${tableLabel} marked as ignored.`,
      );
      handleClose();
    } catch (error) {
      setSubmitError(
        getErrorMessage(
          error as RTKErrorResult["error"],
          "Failed to record the reconciliation. Please try again.",
        ),
      );
    }
  };

  return (
    <ConfirmCloseModal
      title={`Reconcile ${tableLabel}`}
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
      <Space orientation="vertical" size="large" className="w-full pt-2">
        <Space orientation="vertical" size="small">
          <Text strong>{tableLabel}</Text>
          <Text type="secondary" size="sm">
            {finding.field_count}{" "}
            {finding.field_count === 1 ? "column" : "columns"} discovered
          </Text>
        </Space>

        <Form
          form={form}
          layout="vertical"
          onFinish={handleSubmit}
          initialValues={{ choice: "ignored", systemId: undefined, reason: "" }}
          onValuesChange={() => setSubmitError(null)}
        >
          <Item name="choice" label="Decision">
            <Radio.Group data-testid="reconcile-choice">
              <Space orientation="vertical">
                <Radio value="mapped" data-testid="reconcile-choice-mapped">
                  This table belongs to a system already in the data map
                </Radio>
                <Radio value="ignored" data-testid="reconcile-choice-ignored">
                  This table holds no personal data
                </Radio>
              </Space>
            </Radio.Group>
          </Item>

          <Item
            noStyle
            shouldUpdate={(prev, curr) => prev.choice !== curr.choice}
          >
            {({ getFieldValue }) => {
              const choice: ReconcileChoice = getFieldValue("choice");
              if (choice !== "mapped") {
                return null;
              }
              return (
                <>
                  {systemCandidates.length === 0 && (
                    <Alert
                      type="warning"
                      showIcon
                      className="mb-4"
                      message="No systems can be selected yet"
                      description="PrivacyCare's discovery API can only offer a system here once at least one other finding has already been mapped to it — there is no route yet that looks up an arbitrary system's internal id by name. Ignore this finding with a reason, or ask engineering to map the first one directly."
                      data-testid="no-system-candidates"
                    />
                  )}
                  <Item
                    name="systemId"
                    label="System"
                    required
                    rules={[
                      {
                        validator: async (_rule, value: string | undefined) => {
                          if (!value) {
                            throw new Error(
                              "Choose the system this table belongs to.",
                            );
                          }
                        },
                      },
                    ]}
                  >
                    <Select
                      aria-label="System"
                      data-testid="reconcile-system-select"
                      placeholder="Choose a system"
                      disabled={systemCandidates.length === 0}
                      options={systemCandidates.map((candidate) => ({
                        value: candidate.systemId,
                        label: candidate.systemName,
                      }))}
                    />
                  </Item>
                  <Alert
                    type="success"
                    showIcon
                    className="mb-4"
                    message={`${tableLabel} will be marked already mapped.`}
                    data-testid="reconcile-consequence-mapped"
                  />
                </>
              );
            }}
          </Item>

          <Item
            noStyle
            shouldUpdate={(prev, curr) => prev.choice !== curr.choice}
          >
            {({ getFieldValue }) => {
              const choice: ReconcileChoice = getFieldValue("choice");
              if (choice !== "ignored") {
                return null;
              }
              return (
                <>
                  <Item
                    name="reason"
                    label="Reason"
                    required
                    rules={[
                      {
                        validator: async (_rule, value: string) => {
                          if (isBlank(value)) {
                            throw new Error(
                              "Give a reason. This is the record that explains why this table holds no personal data.",
                            );
                          }
                        },
                      },
                    ]}
                  >
                    <TextArea
                      aria-label="Reason"
                      data-testid="reconcile-reason"
                      rows={3}
                      placeholder="Why does this table hold no personal data?"
                    />
                  </Item>
                  <Alert
                    type="info"
                    showIcon
                    className="mb-4"
                    message={`${tableLabel} will be marked ignored. A reason is required.`}
                    data-testid="reconcile-consequence-ignored"
                  />
                </>
              );
            }}
          </Item>

          {/* Same placement DESIGN.md requires for the screening decision's
              own permanence caution: above the submit button, never after
              it. */}
          <Alert
            type="warning"
            showIcon
            className="mb-4"
            message="A reconciliation is a permanent record."
            description="It cannot be edited or deleted. Reconciling this table again adds a new entry and leaves this one in place — the history shows every decision, who made it, and when."
            data-testid="reconcile-permanence-caution"
          />

          {submitError && (
            <Alert
              type="error"
              showIcon
              className="mb-4"
              message="Could not record the reconciliation"
              description={submitError}
            />
          )}

          <Item noStyle shouldUpdate>
            {({ getFieldValue, getFieldsError }) => {
              const choice: ReconcileChoice = getFieldValue("choice");
              const systemId: string | undefined = getFieldValue("systemId");
              const reason: string = getFieldValue("reason") ?? "";
              const blocked = choice === "mapped" ? !systemId : isBlank(reason);
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
                    loading={isSaving}
                    disabled={blocked || hasFieldErrors}
                    data-testid="reconcile-submit"
                  >
                    Record reconciliation
                  </Button>
                </Flex>
              );
            }}
          </Item>
        </Form>
      </Space>
    </ConfirmCloseModal>
  );
};
