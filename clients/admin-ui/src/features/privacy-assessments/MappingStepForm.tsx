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
import { useEffect, useMemo, useRef, useState } from "react";

import DataCategorySelect from "~/features/common/dropdown/DataCategorySelect";
import DataSubjectSelect from "~/features/common/dropdown/DataSubjectSelect";
import DataUseSelect from "~/features/common/dropdown/DataUseSelect";
import { getErrorMessage } from "~/features/common/helpers";
import useTaxonomies from "~/features/common/hooks/useTaxonomies";
import { useHasPermission } from "~/features/common/Restrict";
import { useGetProcessingGroundsQuery } from "~/features/privacycare/processing-grounds.slice";
import { ScopeRegistryEnum } from "~/types/api";
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
  /** Fix wave, item I2: this step owns its own Form instance, so the
   * containing modal's close-confirmation guard cannot read this step's
   * dirty state the way it reads the decision step's (`form.isFieldsTouched()`
   * on a form it owns directly). Called with `true` on the first genuine
   * user edit — never for the one-time prefill of an existing mapping —
   * so the modal can track this step's own dirtiness. */
  onDirtyChange?: (isDirty: boolean) => void;
}

export const MappingStepForm = ({
  businessProcessId,
  processName,
  hasMappingFromList,
  onSaved,
  onCancel,
  onSavingChange,
  onDirtyChange,
}: MappingStepFormProps) => {
  const message = useMessage();
  const [form] = Form.useForm<MappingFormValues>();
  const [submitError, setSubmitError] = useState<string | null>(null);
  // Fix wave, item I2: guards the one-time `form.setFieldsValue` below from
  // being mistaken for a user edit. AntD fires `onValuesChange` for a
  // programmatic `setFieldsValue` exactly the same way it fires for typing,
  // so without this a reopened, pre-filled mapping would read as "dirty"
  // before she has touched anything — a false-positive discard warning on
  // plain Escape, the opposite failure from the bug this guard exists for.
  const isHydratingRef = useRef(false);

  // M7: the POST route requires SYSTEM_UPDATE in addition to
  // PRIVACYCARE_SCREENING_CREATE (it writes ctl_systems and
  // privacydeclaration, Ethyca tables this scope alone was never meant to
  // touch) — see screening/mapping.py's own module docstring, "fix round 2,
  // item I-3". PRIVACYCARE_SCREENING_CREATE is already guaranteed by the
  // time this component renders (every path into it — the table's mapping
  // action and the decision step's "applicable" transition — is itself
  // behind that scope), so SYSTEM_UPDATE is the one gate this form still
  // has to check for itself before offering a Save button that would 403.
  const canSaveMapping = useHasPermission([ScopeRegistryEnum.SYSTEM_UPDATE]);

  const {
    data: mappingData,
    isLoading: isLoadingMapping,
    isError: isMappingError,
    error: mappingError,
    refetch: refetchMapping,
  } = useGetDataMappingQuery(businessProcessId);

  const { getDataCategories, isLoading: isLoadingTaxonomies } = useTaxonomies();
  const allDataCategories = getDataCategories();

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
  // I3: the filter above is correct to keep — but silently dropping 12 of
  // 23 real business situations (including "Performance of a Contract",
  // the likeliest basis for most of a fuel retailer's customer processing)
  // is not something the picker gets to leave unexplained. `unmapped_count`
  // is the same count grounds.py's own ProcessingGroundListResponse already
  // carries for exactly this purpose (D-KT-4).
  const totalGroundsCount =
    (groundsData?.grounds.length ?? 0) + (groundsData?.unmapped_count ?? 0);

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
    isHydratingRef.current = true;
    // I1: `existingMapping.ground` is now resolved by get_mapping through
    // privacycare_declaration_ground -> privacycare_processing_ground,
    // rather than always null — this lookup finding a match in
    // usableGrounds is what makes the picker (and the derived-legal-basis
    // line below it) actually prefill.
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
    isHydratingRef.current = false;
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
      onValuesChange={() => {
        setSubmitError(null);
        if (!isHydratingRef.current) {
          onDirtyChange?.(true);
        }
      }}
    >
      <Space orientation="vertical" size="middle" className="w-full">
        <Text type="secondary" size="sm">
          Every answer here can be saved as-is and finished later — only a name
          and at least one data category are required.
        </Text>

        {/* M2: DESIGN.md's own caution for this step — saving here is not a
            draft with no consequence, it creates or updates a real Fides
            processing activity other parts of the product already build
            on. */}
        <Alert
          type="warning"
          showIcon
          message="Saving creates a processing activity used elsewhere"
          description="Saving this mapping creates (or updates) a processing activity that becomes visible elsewhere in the product and that assessments can be generated against. Changing it later changes what those assessments were built from."
          data-testid="mapping-caution"
        />

        <Item
          name="name"
          label="Activity name"
          tooltip="Defaults to the business process's own name."
          rules={[{ required: true, message: "An activity name is required" }]}
        >
          <Input aria-label="Activity name" data-testid="input-mapping-name" />
        </Item>

        {/* C1: DataCategorySelect/DataSubjectSelect (an existing internal
            wrapper around TaxonomySelect, already used elsewhere — e.g.
            AddEditAssetModal.tsx, ConditionValuesField.tsx) replace
            Ethyca's own DataCategoriesFormItem/DataSubjectsFormItem here.
            Those inherited components label and filter every option by its
            raw fides_key ("user.demographic.religious_belief"), which she
            cannot search — DataCategorySelect/DataSubjectSelect show the
            taxonomy's own human name ("Religion") as the primary label,
            with the fides_key as secondary text, and filter on BOTH. Wrapped
            rather than forked or edited in place: the Ethyca-authored
            System form keeps using DataCategoriesFormItem/
            DataSubjectsFormItem completely unchanged. */}
        <Item
          name="data_categories"
          label="Data categories"
          tooltip="What type of data is your system processing? This could be various types of user or system data."
          rules={[
            {
              required: true,
              type: "array",
              min: 1,
              message: "Must assign at least one data category",
            },
          ]}
        >
          <DataCategorySelect
            aria-label="Data categories"
            mode="multiple"
            selectedTaxonomies={[]}
            variant="outlined"
            autoFocus={false}
            data-testid="input-data_categories"
          />
        </Item>
        <Item
          name="data_subjects"
          label="Data subjects"
          tooltip="Whose data are you processing? This could be customers, employees or any other type of user in your system."
        >
          <DataSubjectSelect
            aria-label="Data subjects"
            mode="multiple"
            selectedTaxonomies={[]}
            variant="outlined"
            autoFocus={false}
            data-testid="input-data_subjects"
          />
        </Item>

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
        {/* I1: falls back to the mapping's own persisted ground/legal basis
            when the picker has no live selection yet — get_mapping now
            resolves and returns both on load, so she sees the law she is
            already operating under, not just the law for whatever she is
            actively choosing this session. */}
        <Item
          noStyle
          shouldUpdate={(prev, curr) => prev.groundId !== curr.groundId}
        >
          {({ getFieldValue }) => {
            const selected = usableGrounds.find(
              (g) => g.id === getFieldValue("groundId"),
            );
            const groundLabel = selected?.ground ?? existingMapping?.ground;
            const legalBasis =
              selected?.fides_legal_basis ?? existingMapping?.fides_legal_basis;
            if (!groundLabel || !legalBasis) {
              return null;
            }
            return (
              <Text size="sm" data-testid="derived-legal-basis">
                {groundLabel} →{" "}
                <Text strong size="sm">
                  {legalBasis}
                </Text>
              </Text>
            );
          }}
        </Item>
        {/* I3: the disclosure the review asked for — the filter above is
            correct to keep, but dropping over half the real business
            situations with no explanation is not. OQ-W2-4 (tracked for the
            SME): which of the missing 12 should be prioritised for a
            lawful-basis ruling — "Performance of a Contract" most of all —
            is a mapping decision, not a disclosure decision, and stays
            open. */}
        <Text
          type="secondary"
          size="sm"
          data-testid="grounds-availability-note"
        >
          {usableGrounds.length} of {totalGroundsCount} business situations are
          available; the rest are awaiting a lawful-basis mapping.
        </Text>

        <Item
          name="purpose"
          label="Purpose of processing"
          tooltip="What is this data processed for? Chosen from the loaded data uses — never free text."
        >
          <DataUseSelect
            aria-label="Purpose of processing"
            data-testid="input-purpose"
            placeholder="Select a purpose"
            allowClear
            selectedTaxonomies={[]}
            variant="outlined"
            autoFocus={false}
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

        {/* M7: explain the dead end before she hits it, rather than letting
            her fill six fields and get a 403 from the server's own
            SYSTEM_UPDATE requirement. */}
        {!canSaveMapping && (
          <Alert
            type="error"
            showIcon
            message="You do not have permission to save this mapping"
            description="Saving a mapping also updates the linked Fides system, which needs the System Update permission in addition to Screening. Ask a Fides administrator to grant it, or ask them to save this mapping for you."
            data-testid="missing-system-update-notice"
          />
        )}

        <Flex justify="end" gap="small">
          <Button onClick={onCancel} disabled={isSaving}>
            Cancel
          </Button>
          <Button
            type="primary"
            htmlType="submit"
            loading={isSaving}
            disabled={!canSaveMapping}
          >
            Save mapping
          </Button>
        </Flex>
      </Space>
    </Form>
  );
};
