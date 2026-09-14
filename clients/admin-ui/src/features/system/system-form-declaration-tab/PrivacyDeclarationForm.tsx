/**
 * antd-based privacy declaration form for the system configure / add-system flow.
 *
 * Composes the shared per-field components in `~/features/system/privacy-declaration-fields/`
 * with the modal-only fields (legal basis + impact assessment, special category, third parties,
 * features, retention period). Validation lives on each Form.Item's `rules` array — no Yup.
 */

import {
  Button,
  Card,
  Flex,
  Form,
  Input,
  Select,
  Spin,
  Switch,
  useMessage,
} from "fidesui";
import { useEffect, useMemo } from "react";

import { useAppSelector } from "~/app/hooks";
import {
  CustomFieldValues,
  useCustomFields,
} from "~/features/common/custom-fields";
import { LegacyResourceTypes } from "~/features/common/custom-fields/types";
import { getErrorMessage } from "~/features/common/helpers";
// PrivacyCare (spec 2026-09-13 D-KT-5)
import {
  useGetDeclarationGroundQuery,
  useSetDeclarationGroundMutation,
} from "~/features/privacycare/processing-grounds.slice";
import { selectLockedForGVL } from "~/features/system/dictionary-form/dict-suggestion.slice";
import {
  DataCategoriesFormItem,
  DatasetReferencesFormItem,
  DataSubjectsFormItem,
  DataUseFormItem,
  DeclarationNameFormItem,
  PrivacyDeclarationCustomFields,
} from "~/features/system/privacy-declaration-fields";
import {
  DataCategory,
  Dataset,
  DataSubject,
  DataUse,
  LegalBasisForProcessingEnum,
  PrivacyDeclarationResponse,
} from "~/types/api";
import { RTKErrorResult } from "~/types/errors/api";

import useLegalBasisOptions from "./useLegalBasisOptions";
import useSpecialCategoryLegalBasisOptions from "./useSpecialCategoryLegalBasisOptions";

const LEGITIMATE_INTERESTS = "Legitimate interests";

export type FormValues = Omit<
  PrivacyDeclarationResponse,
  "cookies" | "legal_basis_for_processing"
> & {
  customFieldValues: CustomFieldValues;
  /**
   * PrivacyCare (spec 2026-09-13 D-KT-5): while the form is open this field
   * holds the SELECTED OPTION's value — a Kenyan processing-ground id for a
   * ground option, or the Article 6 class itself for a class-only option.
   * `handleFinish` substitutes the option's class back in before the system
   * PUT, so what is stored in `privacydeclaration.legal_basis_for_processing`
   * is always the enum (D-KT-4).
   */
  legal_basis_for_processing?: string | null;
};

const defaultInitialValues: FormValues = {
  name: "",
  data_categories: [],
  data_use: "",
  data_subjects: [],
  egress: undefined,
  ingress: undefined,
  features: [],
  legal_basis_for_processing: undefined,
  flexible_legal_basis_for_processing: true,
  impact_assessment_location: "",
  retention_period: "",
  processes_special_category_data: false,
  special_category_legal_basis: undefined,
  data_shared_with_third_parties: false,
  third_parties: "",
  shared_categories: [],
  customFieldValues: {},
  id: "",
};

const transformFormValueToDeclaration = (
  values: FormValues,
  // PrivacyCare (spec 2026-09-13 D-KT-5): the Article 6 class the selected
  // option resolves to. D-KT-4: the stored column is always the enum, never
  // a ground id — the ground itself is recorded separately, in
  // privacycare_declaration_ground.
  legalBasis: LegalBasisForProcessingEnum | null | undefined,
): PrivacyDeclarationResponse => {
  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  const { customFieldValues, ...rest } = values;
  return {
    ...rest,
    legal_basis_for_processing: legalBasis,
    // fill in an empty string for name: https://github.com/ethyca/fideslang/issues/98
    name: values.name ?? "",
    special_category_legal_basis: values.processes_special_category_data
      ? values.special_category_legal_basis
      : undefined,
    third_parties: values.data_shared_with_third_parties
      ? values.third_parties
      : undefined,
    shared_categories: values.data_shared_with_third_parties
      ? values.shared_categories
      : undefined,
  };
};

export const transformPrivacyDeclarationToFormValues = (
  privacyDeclaration?: PrivacyDeclarationResponse,
  customFieldValues?: CustomFieldValues,
): FormValues =>
  privacyDeclaration
    ? {
        ...privacyDeclaration,
        customFieldValues: customFieldValues ?? {},
      }
    : defaultInitialValues;

export interface DataProps {
  allDataCategories: DataCategory[];
  allDataUses: DataUse[];
  allDataSubjects: DataSubject[];
  allDatasets?: Dataset[];
  includeCustomFields?: boolean;
}

interface Props {
  onSubmit: (
    values: PrivacyDeclarationResponse,
  ) => Promise<PrivacyDeclarationResponse[] | undefined>;
  onCancel: () => void;
  initialValues?: PrivacyDeclarationResponse;
  /** Fires whenever the form transitions to a dirty state. */
  onDirtyChange?: (dirty: boolean) => void;
}

export const PrivacyDeclarationForm = ({
  onSubmit,
  onCancel,
  initialValues: passedInInitialValues,
  onDirtyChange,
  allDataUses,
  allDataCategories,
  allDataSubjects,
  allDatasets,
  includeCustomFields,
}: Props & DataProps) => {
  const privacyDeclarationId = passedInInitialValues?.id;
  const isEditing = !!privacyDeclarationId;
  const lockedForGVL = useAppSelector(selectLockedForGVL);

  const { legalBasisOptions } = useLegalBasisOptions();
  const { specialCategoryLegalBasisOptions } =
    useSpecialCategoryLegalBasisOptions();

  // PrivacyCare (spec 2026-09-13 D-KT-5): which Kenyan ground this
  // declaration was recorded against, so an edit opens the dropdown on the
  // ground the consultant actually chose. 404 (no ground recorded — every
  // declaration written before this screen existed, and every one Fides
  // re-created under a new id) leaves `data` undefined and the form falls
  // back to the stored Article 6 class, which is a class-only option.
  // `refetchOnMountOrArgChange` because the answer changes underneath the
  // RTK cache every time a ground is recorded: without it, re-opening the
  // same declaration inside the cache window would replay the 404 from
  // before the save.
  const { data: recordedGround, isFetching: isGroundFetching } =
    useGetDeclarationGroundQuery(privacyDeclarationId ?? "", {
      skip: !privacyDeclarationId,
      refetchOnMountOrArgChange: true,
    });
  const [setDeclarationGround] = useSetDeclarationGroundMutation();
  // PrivacyCare (spec 2026-09-13 D-KT-5): same toast helper
  // useSystemDataUseCrud's handleResult uses for the system-save error path.
  const message = useMessage();

  const { customFieldValues, upsertCustomFields, isLoading } = useCustomFields({
    resourceType: LegacyResourceTypes.PRIVACY_DECLARATION,
    resourceFidesKey: privacyDeclarationId,
  });

  const initialValues = useMemo(() => {
    const values = transformPrivacyDeclarationToFormValues(
      passedInInitialValues,
      customFieldValues,
    );
    // PrivacyCare (spec 2026-09-13 D-KT-5): seed the select from the
    // RECORDED ground when there is one. The stored column holds the
    // Article 6 class, which several grounds share — selecting by class
    // would render the alphabetically-first ground carrying it, i.e. a
    // ground nobody chose. antd only reads `initialValues` when the Form
    // mounts, which is why the render below waits for this query to settle.
    return recordedGround
      ? {
          ...values,
          legal_basis_for_processing: recordedGround.processing_ground_id,
        }
      : values;
  }, [passedInInitialValues, customFieldValues, recordedGround]);

  // PrivacyCare (spec 2026-09-13 D-KT-5): the Article 6 class an option
  // value resolves to. A ground id resolves to its ground's class; a
  // class-only option resolves to itself.
  const legalBasisClassOf = (
    value: string | null | undefined,
  ): LegalBasisForProcessingEnum | null | undefined =>
    (legalBasisOptions.find((option) => option.value === value)?.legalBasis ??
      value) as LegalBasisForProcessingEnum | null | undefined;

  const [form] = Form.useForm<FormValues>();

  // PrivacyCare (spec 2026-09-13 D-KT-5): antd reads `initialValues` once,
  // when the Form mounts. The recorded ground can arrive after that — RTK
  // Query hands back a cached answer (or none) on the first render and
  // resolves the real one a tick later — and an initialValues recomputation
  // alone would never reach the rendered Select, which is how a re-opened
  // declaration could still show the ground it was recorded against BEFORE
  // the last save. Push it into the field instead, and only while the
  // consultant has not touched the dropdown herself: her in-flight choice
  // always wins over a late server answer.
  useEffect(() => {
    if (!recordedGround) {
      return;
    }
    if (form.isFieldTouched("legal_basis_for_processing")) {
      return;
    }
    form.setFieldValue(
      "legal_basis_for_processing",
      recordedGround.processing_ground_id,
    );
  }, [recordedGround, form]);

  const handleFinish = async (values: FormValues) => {
    // PrivacyCare (spec 2026-09-13 D-KT-5): the selected option — a Kenyan
    // ground or a bare Article 6 class. Its `legalBasis` is what the system
    // PUT writes (D-KT-4: the stored column is always the enum); its
    // `groundId`, when it has one, is what gets recorded against the
    // declaration afterwards.
    const selectedOption = legalBasisOptions.find(
      (option) => option.value === values.legal_basis_for_processing,
    );
    // antd Form only tracks fields with a Form.Item; untracked fields
    // (`id`, `egress`, `ingress`) are silently dropped from `values`. Merge
    // them back in from initialValues so updates aren't mistaken for creates.
    const declaration = transformFormValueToDeclaration(
      {
        ...initialValues,
        ...values,
      },
      legalBasisClassOf(values.legal_basis_for_processing),
    );
    const success = await onSubmit(declaration);
    if (success) {
      const matched = success.find(
        (pd) =>
          pd.data_use === values.data_use &&
          (pd.name ? pd.name === values.name : true),
      );
      if (matched?.id) {
        await upsertCustomFields({
          customFieldValues: values.customFieldValues,
          fides_key: matched.id,
        });
        // PrivacyCare (spec 2026-09-13 D-KT-5): record which Kenyan ground
        // justified this declaration's legal basis, now that the
        // declaration id is known from the system save response. This fires
        // on EVERY save with a ground selected, not only when the dropdown
        // was touched: Fides matches declarations on the logical id
        // `data_use:name` (db/system.py), so an edit that changes either
        // one DELETES the old declaration row and CREATES a new one with a
        // new id — a ground recorded against the old id would be stranded
        // and the new declaration would carry none. Only class-only options
        // have no groundId, so there is nothing to record for those.
        //
        // The system save above has already succeeded and is NOT rolled
        // back on a ground-recording failure — that would undo a real save
        // over a secondary, correctable step. Instead .unwrap() the
        // mutation so a failure (e.g. the 422 class-mismatch the API
        // raises, or a network error) surfaces the same way
        // useSystemDataUseCrud's handleResult reports a failed system
        // save: message.error with getErrorMessage's extracted detail,
        // falling back to a message that tells the consultant exactly what
        // to do next rather than a generic "something went wrong".
        if (selectedOption?.groundId) {
          try {
            await setDeclarationGround({
              id: matched.id,
              processing_ground_id: selectedOption.groundId,
            }).unwrap();
          } catch (error) {
            message.error(
              getErrorMessage(
                error as RTKErrorResult["error"],
                "The legal basis ground was not recorded. Please re-select it and save again.",
              ),
            );
          }
        }
      }
    }
  };

  // PrivacyCare (spec 2026-09-13 D-KT-5): `isGroundFetching` joins the
  // existing custom-fields gate because antd reads `initialValues` once, at
  // mount — mounting before the recorded ground is known would open the
  // dropdown on the stored class and never correct itself.
  if (isEditing && (isLoading || isGroundFetching)) {
    return (
      <Flex justify="center" align="center" className="py-8">
        <Spin />
      </Flex>
    );
  }

  return (
    <Form
      form={form}
      layout="vertical"
      key={privacyDeclarationId ?? "new"}
      initialValues={initialValues}
      onFinish={handleFinish}
      onValuesChange={() => onDirtyChange?.(true)}
      data-testid="declaration-form"
    >
      <Flex vertical gap="middle">
        <Card size="small" title="Data use declaration">
          <DeclarationNameFormItem
            disabled={isEditing}
            label="Declaration name (optional)"
            tooltip="Would you like to append anything to the system name?"
          />
          <DataUseFormItem
            allDataUses={allDataUses}
            disabled={isEditing}
            tooltip="For which business purposes is this data processed?"
          />
          <DataCategoriesFormItem
            allDataCategories={allDataCategories}
            disabled={lockedForGVL}
            required
            tooltip="Which categories of personal data are collected for this purpose?"
          />
          <DataSubjectsFormItem
            allDataSubjects={allDataSubjects}
            tooltip="Who are the subjects for this personal data?"
          />
          <Form.Item
            name="legal_basis_for_processing"
            label="Legal basis for processing"
            tooltip="What is the legal basis under which personal data is processed for this purpose?"
          >
            <Select
              aria-label="Legal basis for processing"
              data-testid="input-legal_basis_for_processing"
              // PrivacyCare (spec 2026-09-13 D-KT-5): `value` is the ground
              // id (class-only options carry the class as their own value),
              // so every option is distinct and antd resolves the displayed
              // label to the ground that was actually chosen. The Article 6
              // class travels beside it on `legalBasis` and is substituted
              // into the system PUT by handleFinish — it is never the
              // option's identity, because several grounds share one class.
              options={legalBasisOptions.map(({ label, value }) => ({
                label,
                value,
              }))}
              // PrivacyCare (spec 2026-09-13 D-KT-5): antd filters typed
              // search text against `value` by default, and `value` is now
              // an opaque ground id — typing "KYC" would match nothing.
              // Filter on the label the consultant can actually read.
              optionFilterProp="label"
              disabled={lockedForGVL}
              allowClear
            />
          </Form.Item>
          <Form.Item
            noStyle
            shouldUpdate={(prev, curr) =>
              prev.legal_basis_for_processing !==
              curr.legal_basis_for_processing
            }
          >
            {({ getFieldValue }) =>
              // PrivacyCare (spec 2026-09-13 D-KT-5): resolve through the
              // option — the field now holds a ground id, and a Kenyan
              // ground classed "Legitimate interests" needs this field just
              // as much as the bare class does.
              legalBasisClassOf(getFieldValue("legal_basis_for_processing")) ===
              LEGITIMATE_INTERESTS ? (
                <Form.Item
                  name="impact_assessment_location"
                  label="Impact assessment location"
                  tooltip="Where is the legitimate interest impact assessment stored?"
                >
                  <Input
                    aria-label="Impact assessment location"
                    data-testid="input-impact_assessment_location"
                  />
                </Form.Item>
              ) : null
            }
          </Form.Item>
          <Form.Item
            name="flexible_legal_basis_for_processing"
            label="This legal basis is flexible"
            tooltip="Has the vendor declared that the legal basis may be overridden?"
            valuePropName="checked"
          >
            <Switch
              disabled={lockedForGVL}
              data-testid="input-flexible_legal_basis_for_processing"
            />
          </Form.Item>
          <Form.Item
            name="retention_period"
            label="Retention period (days)"
            tooltip="How long is personal data retained for this purpose?"
            className="mb-0"
          >
            <Input
              aria-label="Retention period"
              data-testid="input-retention_period"
              disabled={lockedForGVL}
            />
          </Form.Item>
        </Card>

        <Card size="small" title="Features">
          <Form.Item
            name="features"
            label="Features"
            tooltip="What are some features of how data is processed?"
            className="mb-0"
          >
            <Select
              aria-label="Features"
              data-testid="input-features"
              mode="tags"
              placeholder="Describe features..."
              disabled={lockedForGVL}
            />
          </Form.Item>
        </Card>

        <Card size="small" title="Dataset reference">
          <DatasetReferencesFormItem
            allDatasets={allDatasets ?? []}
            tooltip="Is there a dataset configured for this system?"
          />
        </Card>

        <Card size="small" title="Special category data">
          <Form.Item
            name="processes_special_category_data"
            label="This system processes special category data"
            tooltip="Is this system processing special category data as defined by GDPR Article 9?"
            valuePropName="checked"
            className="mb-0"
          >
            <Switch data-testid="input-processes_special_category_data" />
          </Form.Item>
          <Form.Item
            noStyle
            shouldUpdate={(prev, curr) =>
              prev.processes_special_category_data !==
              curr.processes_special_category_data
            }
          >
            {({ getFieldValue }) =>
              getFieldValue("processes_special_category_data") ? (
                <Form.Item
                  name="special_category_legal_basis"
                  label="Legal basis for processing"
                  tooltip="What is the legal basis under which the special category data is processed?"
                  className="mb-0 mt-4"
                  rules={[
                    {
                      required: true,
                      message: "Legal basis for processing is required",
                    },
                  ]}
                >
                  <Select
                    aria-label="Special category legal basis"
                    data-testid="input-special_category_legal_basis"
                    options={specialCategoryLegalBasisOptions}
                    allowClear
                  />
                </Form.Item>
              ) : null
            }
          </Form.Item>
        </Card>

        <Card size="small" title="Third parties">
          <Form.Item
            name="data_shared_with_third_parties"
            label="This system shares data with 3rd parties for this purpose"
            tooltip="Does this system disclose, sell, or share personal data collected for this business use with 3rd parties?"
            valuePropName="checked"
            className="mb-0"
          >
            <Switch data-testid="input-data_shared_with_third_parties" />
          </Form.Item>
          <Form.Item
            noStyle
            shouldUpdate={(prev, curr) =>
              prev.data_shared_with_third_parties !==
              curr.data_shared_with_third_parties
            }
          >
            {({ getFieldValue }) =>
              getFieldValue("data_shared_with_third_parties") ? (
                <Flex vertical gap="middle" className="mt-4">
                  <Form.Item
                    name="third_parties"
                    label="Third parties"
                    tooltip="Which type of third parties is the data shared with?"
                    className="mb-0"
                  >
                    <Input
                      aria-label="Third parties"
                      data-testid="input-third_parties"
                    />
                  </Form.Item>
                  <Form.Item
                    name="shared_categories"
                    label="Shared categories"
                    tooltip="Which categories of personal data does this system share with third parties?"
                    className="mb-0"
                  >
                    <Select
                      aria-label="Shared categories"
                      data-testid="input-shared_categories"
                      mode="multiple"
                      options={allDataCategories.map((c) => ({
                        value: c.fides_key,
                        label: c.fides_key,
                      }))}
                    />
                  </Form.Item>
                </Flex>
              ) : null
            }
          </Form.Item>
        </Card>

        {includeCustomFields ? (
          <PrivacyDeclarationCustomFields
            privacyDeclarationId={privacyDeclarationId}
          />
        ) : null}

        <Flex justify="end" align="center" gap="small">
          <Button onClick={onCancel} data-testid="cancel-btn">
            Cancel
          </Button>
          <Form.Item shouldUpdate noStyle>
            {() => (
              <Button
                type="primary"
                htmlType="submit"
                data-testid="save-btn"
                disabled={
                  !form.isFieldsTouched() ||
                  form.getFieldsError().some((field) => field.errors.length > 0)
                }
              >
                Save
              </Button>
            )}
          </Form.Item>
        </Flex>
      </Flex>
    </Form>
  );
};
