import {
  Alert,
  Button,
  Flex,
  Form,
  Input,
  Result,
  Select,
  Space,
  Spin,
  Text,
  useMessage,
} from "fidesui";
import { useEffect, useMemo, useState } from "react";

import { getErrorMessage } from "~/features/common/helpers";
import useTaxonomies from "~/features/common/hooks/useTaxonomies";
import { useGetProcessingGroundsQuery } from "~/features/privacycare/processing-grounds.slice";
import {
  DataCategoriesFormItem,
  DataSubjectsFormItem,
} from "~/features/system/privacy-declaration-fields";
import { isAPIError, RTKErrorResult } from "~/types/errors/api";

import { KENYAN_SPECIAL_CATEGORY_DESCRIPTION } from "./screening.constants";
import {
  useGetDataMappingQuery,
  useSaveDataMappingMutation,
} from "./screening.slice";
import { anySpecialCategoryKey } from "./screening.utils";

const { Item } = Form;

interface MappingFormValues {
  name: string;
  data_categories: string[];
  data_subjects?: string[];
  groundId?: string;
  purpose?: string;
  retention_period?: string;
  third_parties?: string;
}

// Optional single-value fields where a value the user typed and then fully
// deleted should read as "not answered", not as a literal empty string
// on the wire — DataMappingRequest's own contract for these four fields is
// None-means-unanswered, and an empty string is not None.
const blankToUndefined = (value?: string): string | undefined =>
  value && value.trim().length > 0 ? value : undefined;

interface MappingStepFormProps {
  businessProcessId: string;
  processName: string;
  /** From the screening list's own `has_mapping` — used to tell "never
   * mapped through this route" apart from "mapped, but by an activity this
   * route does not own" when the mapping read itself comes back null. */
  hasMappingFromList: boolean;
  onSaved: () => void;
  onCancel: () => void;
  /** While true, the containing modal must not be dismissible. */
  onSavingChange?: (isSaving: boolean) => void;
}

export const MappingStepForm = ({
  businessProcessId,
  processName,
  hasMappingFromList,
  onSaved,
  onCancel,
  onSavingChange,
}: MappingStepFormProps) => {
  const message = useMessage();
  const [form] = Form.useForm<MappingFormValues>();
  const [submitError, setSubmitError] = useState<string | null>(null);

  const {
    data: mappingData,
    isLoading: isLoadingMapping,
    isError: isMappingError,
    error: mappingError,
    refetch: refetchMapping,
  } = useGetDataMappingQuery(businessProcessId);

  const {
    getDataCategories,
    getDataUses,
    getDataSubjects,
    isLoading: isLoadingTaxonomies,
  } = useTaxonomies();
  const allDataCategories = getDataCategories();
  const allDataUses = getDataUses();
  const allDataSubjects = getDataSubjects();

  const { data: groundsData, isLoading: isLoadingGrounds } =
    useGetProcessingGroundsQuery();
  // Only offer grounds that actually resolve to a legal basis. D-KT-4: 12 of
  // the 23 loaded grounds have none yet (nothing Carol has ruled on) — the
  // save route rejects those exactly as grounds.py's own
  // _record_declaration_ground does, so offering them here would only ever
  // produce a guaranteed-to-fail choice.
  const usableGrounds = useMemo(
    () => (groundsData?.grounds ?? []).filter((g) => !!g.fides_legal_basis),
    [groundsData],
  );

  const categoriesByKey = useMemo(
    () => new Map(allDataCategories.map((c) => [c.fides_key, c])),
    [allDataCategories],
  );

  const [saveDataMapping, { isLoading: isSaving }] =
    useSaveDataMappingMutation();

  useEffect(() => {
    onSavingChange?.(isSaving);
  }, [isSaving, onSavingChange]);

  const existingMapping = mappingData?.mapping ?? null;
  // A null mapping alongside has_mapping=true on the list is the third,
  // honest state this screen must never paper over with an empty form: the
  // process has an activity somebody else authored, which this screen must
  // not edit (see screening.py's own module docstring).
  const isOwnedElsewhere = !existingMapping && hasMappingFromList;

  useEffect(() => {
    if (!existingMapping) {
      return;
    }
    const groundId = existingMapping.ground
      ? usableGrounds.find((g) => g.ground === existingMapping.ground)?.id
      : undefined;
    form.setFieldsValue({
      name: existingMapping.name,
      data_categories: existingMapping.data_categories,
      data_subjects: existingMapping.data_subjects,
      groundId,
      purpose: existingMapping.purpose ?? undefined,
      retention_period: existingMapping.retention_period ?? undefined,
      third_parties: existingMapping.third_parties ?? undefined,
    });
    // Only when the mapping first loads — never re-run mid-edit, or a
    // background refetch would clobber values the officer is still typing.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [existingMapping]);

  const handleSubmit = async (values: MappingFormValues) => {
    setSubmitError(null);
    const selectedGround = usableGrounds.find((g) => g.id === values.groundId);
    try {
      const result = await saveDataMapping({
        businessProcessId,
        body: {
          name: values.name,
          data_categories: values.data_categories,
          data_subjects: values.data_subjects,
          ground: selectedGround?.ground,
          purpose: blankToUndefined(values.purpose),
          retention_period: blankToUndefined(values.retention_period),
          third_parties: blankToUndefined(values.third_parties),
        },
      }).unwrap();
      message.success(
        result.processes_special_category_data
          ? `Mapping saved for ${processName}. It includes special category data under the Data Protection Act 2019 §2 — recorded automatically.`
          : `Mapping saved for ${processName}.`,
      );
      onSaved();
    } catch (error) {
      setSubmitError(
        getErrorMessage(
          error as RTKErrorResult["error"],
          "Failed to save the mapping. Please try again.",
        ),
      );
    }
  };

  if (isLoadingMapping || isLoadingTaxonomies || isLoadingGrounds) {
    return (
      <Flex justify="center" align="center" className="py-8">
        <Spin />
      </Flex>
    );
  }

  if (isMappingError) {
    const isNotFound = isAPIError(mappingError) && mappingError.status === 404;
    return (
      <Result
        status={isNotFound ? "warning" : "error"}
        title={
          isNotFound
            ? "This business process could not be found."
            : "Failed to load the existing mapping."
        }
        subTitle={isNotFound ? undefined : "Please try again."}
        extra={
          isNotFound ? undefined : (
            <Button type="primary" onClick={() => refetchMapping()}>
              Retry
            </Button>
          )
        }
      />
    );
  }

  if (isOwnedElsewhere) {
    return (
      <Space orientation="vertical" size="middle" className="w-full">
        <Alert
          type="info"
          showIcon
          message="This process already has a data mapping"
          description={
            <>
              <Text>
                {processName} links to a processing activity that was created
                somewhere else in the system, not through this screen.
              </Text>
              <Text className="mt-2 block">
                Editing it here isn&apos;t possible without risking a duplicate
                — this screen only edits mappings it created itself. Ask a Fides
                administrator to update the activity directly, or contact
                PrivacyCare support if you believe this is wrong.
              </Text>
            </>
          }
        />
        <Flex justify="end">
          <Button onClick={onCancel}>Close</Button>
        </Flex>
      </Space>
    );
  }

  return (
    <Form
      form={form}
      layout="vertical"
      onFinish={handleSubmit}
      initialValues={{ name: processName }}
      onValuesChange={() => setSubmitError(null)}
    >
      <Space orientation="vertical" size="middle" className="w-full">
        <Text type="secondary" size="sm">
          Every answer here can be saved as-is and finished later — only a name
          and at least one data category are required.
        </Text>

        <Item
          name="name"
          label="Activity name"
          tooltip="Defaults to the business process's own name."
          rules={[{ required: true, message: "An activity name is required" }]}
        >
          <Input aria-label="Activity name" data-testid="input-mapping-name" />
        </Item>

        <DataCategoriesFormItem
          allDataCategories={allDataCategories}
          required
        />
        <DataSubjectsFormItem allDataSubjects={allDataSubjects} />

        <Item
          noStyle
          shouldUpdate={(prev, curr) =>
            prev.data_categories !== curr.data_categories
          }
        >
          {({ getFieldValue }) => {
            const selected: string[] = getFieldValue("data_categories") ?? [];
            return anySpecialCategoryKey(selected, categoriesByKey) ? (
              <Alert
                type="warning"
                showIcon
                message="Includes special category data"
                description={`One or more chosen data categories fall under the Data Protection Act 2019 §2 special categories — ${KENYAN_SPECIAL_CATEGORY_DESCRIPTION}. PrivacyCare records this automatically; no further action is needed on this form.`}
              />
            ) : null;
          }}
        </Item>

        <Item
          name="groundId"
          label="Lawful basis"
          tooltip="Choose the business situation. PrivacyCare supplies the legal basis it maps to."
        >
          <Select
            aria-label="Lawful basis"
            data-testid="input-ground"
            placeholder="Select the business situation that applies"
            allowClear
            showSearch
            optionFilterProp="label"
            options={usableGrounds.map((g) => ({
              value: g.id,
              label: g.ground,
            }))}
          />
        </Item>
        <Item
          noStyle
          shouldUpdate={(prev, curr) => prev.groundId !== curr.groundId}
        >
          {({ getFieldValue }) => {
            const selected = usableGrounds.find(
              (g) => g.id === getFieldValue("groundId"),
            );
            return selected ? (
              <Text size="sm" data-testid="derived-legal-basis">
                {selected.ground} →{" "}
                <Text strong size="sm">
                  {selected.fides_legal_basis}
                </Text>
              </Text>
            ) : null;
          }}
        </Item>

        <Item
          name="purpose"
          label="Purpose of processing"
          tooltip="What is this data processed for? Chosen from the loaded data uses — never free text."
        >
          <Select
            aria-label="Purpose of processing"
            data-testid="input-purpose"
            placeholder="Select a purpose"
            allowClear
            showSearch
            optionFilterProp="label"
            options={allDataUses.map((du) => ({
              value: du.fides_key,
              label: du.fides_key,
            }))}
          />
        </Item>

        <Item name="retention_period" label="Retention">
          <Input
            aria-label="Retention"
            data-testid="input-retention_period"
            placeholder="e.g. 7 years"
          />
        </Item>

        <Item name="third_parties" label="Third-party processors">
          <Input
            aria-label="Third-party processors"
            data-testid="input-third_parties"
            placeholder="e.g. none, or name the processor"
          />
        </Item>

        {submitError && (
          <Alert
            type="error"
            showIcon
            message="Could not save the mapping"
            description={submitError}
          />
        )}

        <Flex justify="end" gap="small">
          <Button onClick={onCancel} disabled={isSaving}>
            Cancel
          </Button>
          <Button type="primary" htmlType="submit" loading={isSaving}>
            Save mapping
          </Button>
        </Flex>
      </Space>
    </Form>
  );
};
