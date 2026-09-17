import {
  TaxonomySelect,
  TaxonomySelectOption,
  TaxonomySelectProps,
} from "~/features/common/dropdown/TaxonomySelect";
import useTaxonomies from "~/features/common/hooks/useTaxonomies";

const DataUseSelect = ({
  selectedTaxonomies,
  showDisabled = false,
  ...props
}: TaxonomySelectProps) => {
  const { getDataUseDisplayNameProps, getDataUses } = useTaxonomies();

  const getActiveDataUses = () => getDataUses().filter((du) => du.active);

  const dataUses = showDisabled ? getDataUses() : getActiveDataUses();

  const options: TaxonomySelectOption[] = dataUses
    .filter((dataUse) => !selectedTaxonomies?.includes(dataUse.fides_key))
    .map((dataUse) => {
      const { name, primaryName } = getDataUseDisplayNameProps(
        dataUse.fides_key,
      );
      return {
        value: dataUse.fides_key,
        name,
        primaryName,
        description: dataUse.description || "",
        // Fix wave round 2, item C1 (purpose picker): DataCategorySelect
        // and DataSubjectSelect both set `label` here; this component
        // never did. TaxonomyOption (this file's own dropdown-item
        // renderer, via TaxonomySelect's optionRender) reads `name`/
        // `primaryName` directly, so the OPEN dropdown always showed the
        // human name regardless — the bug was invisible while picking.
        // But AntD's single-select closed/selected display renders
        // `option.label`, not `optionRender`'s output, so a value loaded
        // without ever being re-picked in this session (exactly what
        // reopening a saved mapping does) fell back to the raw fides_key
        // ("essential.legal_obligation") because there was no label to
        // show. Matches the sibling components' construction exactly, so
        // the fix reaches every consumer of this shared component, not
        // just the purpose picker that surfaced it.
        label: (
          <>
            <strong>{primaryName || name}</strong>
            {primaryName && `: ${name}`}
          </>
        ),
        title: dataUse.fides_key,
      };
    });

  return <TaxonomySelect options={options} {...props} />;
};

export default DataUseSelect;
