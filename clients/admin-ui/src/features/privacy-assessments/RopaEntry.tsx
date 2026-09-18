import {
  Alert,
  Button,
  Descriptions,
  Divider,
  Flex,
  Icons,
  Result,
  Skeleton,
  Space,
  Tag,
  Text,
  Tooltip,
  Typography,
  useMessage,
} from "fidesui";
import NextLink from "next/link";

import { getErrorMessage } from "~/features/common/helpers";
import useTaxonomies from "~/features/common/hooks/useTaxonomies";
import { PRIVACY_ASSESSMENTS_SCREENING_ROUTE } from "~/features/common/nav/routes";
import { isAPIError, RTKErrorResult } from "~/types/errors/api";

import {
  downloadCsv,
  ropaCsvFilename,
  ropaEntryToCsvRows,
  ropaRowsToCsv,
} from "./ropa.csv";
import { useGetProcessRopaQuery } from "./ropa.slice";
import { RopaDeclarationResponse } from "./ropa.types";

const { Title } = Typography;

/** A taxonomy key shown as its human name, with the raw key kept
 * discoverable on hover — the same "human name is the headline, the raw
 * identifier stays reachable, never hidden entirely" pattern
 * ScreeningTable.tsx's own DecidedBy component documents for
 * decided_by/decided_by_display. DESIGN.md, Screen 3: "In her language...
 * Never a raw fides key." */
const HumanNameTag = ({
  fidesKey,
  name,
}: {
  fidesKey: string;
  name: string;
}) => (
  <Tooltip title={fidesKey}>
    <Tag>{name}</Tag>
  </Tooltip>
);

const ActivityBlock = ({
  declaration,
  names,
}: {
  declaration: RopaDeclarationResponse;
  names: {
    dataUseName: (key: string) => string;
    dataCategoryName: (key: string) => string;
    dataSubjectName: (key: string) => string;
  };
}) => (
  <div
    className="rounded border p-4"
    data-testid={`ropa-activity-${declaration.id}`}
  >
    <Text strong className="block">
      {declaration.name ?? "Untitled processing activity"}
    </Text>
    <Descriptions column={1} size="small" className="mt-2">
      <Descriptions.Item label="Purpose">
        {names.dataUseName(declaration.data_use)}
      </Descriptions.Item>
      <Descriptions.Item label="Data categories">
        {declaration.data_categories.length === 0 ? (
          <Text type="secondary">None recorded</Text>
        ) : (
          <Space wrap size={[4, 4]}>
            {declaration.data_categories.map((key) => (
              <HumanNameTag
                key={key}
                fidesKey={key}
                name={names.dataCategoryName(key)}
              />
            ))}
          </Space>
        )}
      </Descriptions.Item>
      <Descriptions.Item label="Data subjects">
        {declaration.data_subjects.length === 0 ? (
          <Text type="secondary">None recorded</Text>
        ) : (
          <Space wrap size={[4, 4]}>
            {declaration.data_subjects.map((key) => (
              <HumanNameTag
                key={key}
                fidesKey={key}
                name={names.dataSubjectName(key)}
              />
            ))}
          </Space>
        )}
      </Descriptions.Item>
      <Descriptions.Item label="Lawful basis">
        {declaration.legal_basis ?? (
          <Text type="secondary">Not yet determined</Text>
        )}
      </Descriptions.Item>
      <Descriptions.Item label="Retention">
        {declaration.retention_period ?? (
          <Text type="secondary">Not recorded</Text>
        )}
      </Descriptions.Item>
      <Descriptions.Item label="System">
        {declaration.system_name ?? (
          <Text type="secondary">No linked system</Text>
        )}
      </Descriptions.Item>
    </Descriptions>
  </div>
);

/**
 * PrivacyCare — Screen 3 of docs/design/privacycare-screens/DESIGN.md.
 *
 * The entry half of the ROPA screen: one business process, and everything
 * it does with personal data, assembled from `GET .../ropa` — no write
 * path exists here by design (DESIGN.md: "the way to change it is to
 * change the mapping"), so this component renders only, and explains why
 * there is nothing to edit rather than leaving an officer to hunt for a
 * missing button.
 */
export const RopaEntry = ({
  businessProcessId,
  onBack,
}: {
  businessProcessId: string;
  onBack: () => void;
}) => {
  const message = useMessage();
  const { getDataUseByKey, getDataCategoryByKey, getDataSubjectByKey } =
    useTaxonomies();
  const { data, isLoading, isError, error, refetch } =
    useGetProcessRopaQuery(businessProcessId);

  const names = {
    dataUseName: (key: string) => getDataUseByKey(key)?.name ?? key,
    dataCategoryName: (key: string) => getDataCategoryByKey(key)?.name ?? key,
    dataSubjectName: (key: string) => getDataSubjectByKey(key)?.name ?? key,
  };

  const handleExportEntry = () => {
    if (!data) {
      return;
    }
    try {
      const csv = ropaRowsToCsv(ropaEntryToCsvRows(data, names));
      downloadCsv(csv, ropaCsvFilename("process"));
      message.success(`Downloaded the ROPA entry for ${data.process.name}.`);
    } catch (exportError) {
      message.error(
        getErrorMessage(
          exportError as RTKErrorResult["error"],
          "Failed to build the CSV export. Please try again.",
        ),
      );
    }
  };

  if (isLoading) {
    return (
      <Space orientation="vertical" size="middle" className="w-full">
        <Skeleton active paragraph={{ rows: 3 }} />
        <Skeleton active paragraph={{ rows: 4 }} />
      </Space>
    );
  }

  if (isError || !data) {
    const isNotFound = !!error && isAPIError(error) && error.status === 404;
    return (
      <Result
        status={isNotFound ? "warning" : "error"}
        title={
          isNotFound
            ? "This business process could not be found."
            : "Failed to load this record of processing activities."
        }
        subTitle={isNotFound ? undefined : "Please try again."}
        extra={
          <Space>
            <Button onClick={onBack}>Back to the register</Button>
            {!isNotFound && (
              <Button type="primary" onClick={() => refetch()}>
                Retry
              </Button>
            )}
          </Space>
        }
      />
    );
  }

  const {
    process,
    declarations,
    missing_declarations: missingDeclarations,
  } = data;
  const hasNoActivities =
    declarations.length === 0 && missingDeclarations.length === 0;

  return (
    <Space orientation="vertical" size="large" className="w-full">
      <Flex justify="space-between" align="center" wrap="wrap" gap="small">
        <Button
          type="link"
          className="p-0"
          icon={<Icons.ArrowLeft />}
          onClick={onBack}
          data-testid="ropa-back-to-list"
        >
          Back to the register
        </Button>
        <Button
          icon={<Icons.Download />}
          data-testid="ropa-export-entry"
          onClick={handleExportEntry}
        >
          Export CSV
        </Button>
      </Flex>

      {/* DESIGN.md, "What it must not claim": her brief says the ROPA
          auto-populates from APPROVED DPIA submissions, and there is no
          approval step in this product. This is stated plainly, near the
          top, every time an entry is viewed — never implied to be an
          approved Article 30 record. */}
      <Alert
        type="info"
        showIcon
        data-testid="ropa-not-approved-notice"
        message="This is not an approved record of processing activities"
        description="It is assembled from the mappings recorded against this business process on Screening, and is as current as they are. There is no approval step in this product."
      />

      <div>
        <Title level={3} className="!mb-2">
          {process.name}
        </Title>
        <Descriptions column={1} size="small" bordered>
          {process.description && (
            <Descriptions.Item label="Description">
              {process.description}
            </Descriptions.Item>
          )}
          <Descriptions.Item label="Business cycle">
            {process.business_cycle ?? "—"}
          </Descriptions.Item>
          <Descriptions.Item label="Owner">
            {process.owner_name
              ? `${process.owner_name}${process.owner_email ? ` (${process.owner_email})` : ""}`
              : "—"}
          </Descriptions.Item>
          <Descriptions.Item
            label={
              <Tooltip title="Her own spreadsheet row reference — how this process ties back to her register.">
                Register reference
              </Tooltip>
            }
          >
            {process.external_ref ?? "—"}
          </Descriptions.Item>
        </Descriptions>
      </div>

      {/* DESIGN.md: "Read-only ... an officer who cannot find an edit
          button should be told why, not left hunting." */}
      <Alert
        type="warning"
        showIcon
        data-testid="ropa-no-write-path-notice"
        message="There is no edit control on this screen"
        description={
          <>
            A record of processing activities is assembled from decisions and
            mappings made elsewhere. To change what you see here, change the
            mapping on{" "}
            <NextLink href={PRIVACY_ASSESSMENTS_SCREENING_ROUTE} passHref>
              Screening
            </NextLink>
            .
          </>
        }
      />

      <Divider className="!my-0" />

      {missingDeclarations.length > 0 && (
        <Alert
          type="error"
          showIcon
          data-testid="ropa-missing-declarations"
          message={`${missingDeclarations.length} link${
            missingDeclarations.length === 1 ? "" : "s"
          } point${missingDeclarations.length === 1 ? "s" : ""} to a processing activity that no longer exists`}
          description={
            <>
              <Text className="block">
                This business process links to the following processing
                {missingDeclarations.length === 1 ? " activity" : " activities"}
                , but no record can be found for{" "}
                {missingDeclarations.length === 1 ? "it" : "them"} any more.
                This is shown rather than hidden — a ROPA with a quiet hole is
                worse than one that admits it.
              </Text>
              <ul className="mt-2">
                {missingDeclarations.map((id) => (
                  <li key={id}>
                    <Text code>{id}</Text>
                  </li>
                ))}
              </ul>
            </>
          }
        />
      )}

      {hasNoActivities ? (
        <Alert
          type="info"
          showIcon
          data-testid="ropa-no-activities-notice"
          message="No processing activity has been recorded for this business process yet"
          description={
            <>
              Once a mapping is saved for it on{" "}
              <NextLink href={PRIVACY_ASSESSMENTS_SCREENING_ROUTE} passHref>
                Screening
              </NextLink>
              , it will appear here automatically.
            </>
          }
        />
      ) : (
        <Space orientation="vertical" size="middle" className="w-full">
          <Title level={4} className="!mb-0">
            Processing activities
          </Title>
          {declarations.map((declaration) => (
            <ActivityBlock
              key={declaration.id}
              declaration={declaration}
              names={names}
            />
          ))}
        </Space>
      )}
    </Space>
  );
};
