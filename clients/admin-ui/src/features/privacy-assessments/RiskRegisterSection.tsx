import {
  Button,
  ColumnsType,
  Flex,
  Icons,
  Result,
  Skeleton,
  Table,
  Text,
  Typography,
} from "fidesui";
import { useMemo, useState } from "react";

import Restrict from "~/features/common/Restrict";
import { ScopeRegistryEnum } from "~/types/api";

import { AddRiskModal } from "./AddRiskModal";
import { PriorConsultationBanner } from "./PriorConsultationBanner";
import { RemoveRiskModal } from "./RemoveRiskModal";
import {
  LIKELIHOOD_LABELS,
  RISK_CATEGORY_LABELS,
  SEVERITY_LABELS,
} from "./risk.constants";
import { useGetOdpcFindingQuery, useListRisksQuery } from "./risk.slice";
import { RiskBand, RiskCategory, RiskResponse } from "./risk.types";
import { RiskBandTag } from "./RiskBandTag";

const { Title } = Typography;

const SKELETON_ROW_COUNT = 3;
// A placeholder row for skeleton rendering only — cast through `unknown`
// because each deliberately does not satisfy RiskResponse otherwise; every
// column's render function checks isLoading before touching any other
// field. Each gets its own synthetic id so Table's rowKey can read `id`
// directly rather than an index (antd warns against keying by index).
const SKELETON_ROWS = Array.from({ length: SKELETON_ROW_COUNT }, (_, i) => ({
  id: `skeleton-${i}`,
})) as unknown as RiskResponse[];

// Module scope, not inline in the summary strip's JSX — an inline function
// with three mutually-exclusive branches was flagged for nested ternaries;
// this also keeps the "loading / error / value" shape reusable and easy to
// scan on its own.
const ConsultationStatus = ({
  isLoading,
  isError,
  required,
  onRetry,
}: {
  isLoading: boolean;
  isError: boolean;
  required: boolean | undefined;
  onRetry: () => void;
}) => {
  if (isLoading) {
    return <Skeleton.Input active size="small" />;
  }
  if (isError) {
    return (
      <Flex align="center" gap="small">
        <Text type="secondary" size="sm">
          Couldn&apos;t load
        </Text>
        <Button size="small" type="link" onClick={onRetry}>
          Retry
        </Button>
      </Flex>
    );
  }
  return (
    <Text
      type={required ? "danger" : "secondary"}
      strong={required}
      data-testid="odpc-status"
    >
      {required ? "Required" : "Not required"}
    </Text>
  );
};

interface RiskRegisterSectionProps {
  assessmentId: string;
}

/**
 * PrivacyCare (spec 2026-09-16 D-W2-7g)
 *
 * DESIGN.md, Screen 2 — the Risks section on the assessment detail page.
 * Records the risks in a DPIA so the assessment's risk band is computed
 * from evidence instead of sitting blank, and gives the ODPC
 * prior-consultation finding something to fire on.
 *
 * THE TRAP THIS COMPONENT IS BUILT TO AVOID: the assessment's own
 * risk_level field (PrivacyAssessmentResponse.risk_level, rendered
 * elsewhere by AssessmentCard.tsx) is a three-value, lossy projection —
 * risk/banding.py's projected_risk_level writes CRITICAL there as "high".
 * This component NEVER reads assessment.risk_level. The band shown here is
 * risks[0]?.band — the top entry of listRisks' own highest-score-first
 * ordering, which register.py's list_risks already sorts that way
 * (`ORDER BY -score, id` in Python, not SQL) — i.e. the true four-value
 * band of the assessment's highest-scoring risk, straight from the risk
 * API's own RiskResponse.band. The ODPC route's own `band` field would be
 * an equally authoritative second source for the SAME number; risks[0] is
 * used instead purely because listRisks is already being fetched for the
 * table, and reading the number from two different queries only invites
 * the two showing different things during a stale-cache moment.
 */
export const RiskRegisterSection = ({
  assessmentId,
}: RiskRegisterSectionProps) => {
  const [isAddOpen, setIsAddOpen] = useState(false);
  const [riskToRemove, setRiskToRemove] = useState<RiskResponse | null>(null);

  const {
    data: riskList,
    isLoading: isLoadingRisks,
    isError: isRisksError,
    refetch: refetchRisks,
  } = useListRisksQuery(assessmentId);

  const {
    data: odpcFinding,
    isLoading: isLoadingOdpc,
    isError: isOdpcError,
    refetch: refetchOdpc,
  } = useGetOdpcFindingQuery(assessmentId);

  const risks = useMemo(() => riskList?.items ?? [], [riskList]);

  // Highest-score-first is the server's own ordering (register.list_risks);
  // this is a read of that order, never a client-side re-sort or re-derive
  // of score/band.
  const highestRisk = risks[0] ?? null;
  const overallBand = highestRisk?.band ?? RiskBand.LOW;

  const isLoading = isLoadingRisks;

  const columns: ColumnsType<RiskResponse> = [
    {
      title: "Category",
      dataIndex: "category",
      key: "category",
      render: (value: RiskCategory) =>
        isLoading ? (
          <Skeleton.Input active size="small" />
        ) : (
          (RISK_CATEGORY_LABELS[value] ?? value)
        ),
    },
    {
      title: "Description",
      dataIndex: "description",
      key: "description",
      render: (value: string) =>
        isLoading ? <Skeleton.Input active size="small" block /> : value,
    },
    {
      title: "Likelihood",
      dataIndex: "likelihood",
      key: "likelihood",
      render: (value: number) =>
        isLoading ? (
          <Skeleton.Input active size="small" />
        ) : (
          `${value} — ${LIKELIHOOD_LABELS[value] ?? ""}`
        ),
    },
    {
      title: "Severity",
      dataIndex: "severity",
      key: "severity",
      render: (value: number) =>
        isLoading ? (
          <Skeleton.Input active size="small" />
        ) : (
          `${value} — ${SEVERITY_LABELS[value] ?? ""}`
        ),
    },
    {
      title: "Score",
      dataIndex: "score",
      key: "score",
      render: (value: number) =>
        isLoading ? <Skeleton.Input active size="small" /> : value,
    },
    {
      title: "Band",
      dataIndex: "band",
      key: "band",
      render: (value: RiskBand) =>
        isLoading ? (
          <Skeleton.Input active size="small" />
        ) : (
          <RiskBandTag band={value} />
        ),
    },
    {
      title: "Action",
      key: "action",
      render: (_value, row) =>
        isLoading ? null : (
          <Restrict scopes={[ScopeRegistryEnum.PRIVACYCARE_RISK_CREATE]}>
            <Button
              size="small"
              danger
              icon={<Icons.TrashCan size={14} />}
              data-testid={`remove-risk-${row.id}`}
              onClick={() => setRiskToRemove(row)}
            >
              Remove
            </Button>
          </Restrict>
        ),
    },
  ];

  if (isRisksError) {
    return (
      <div>
        <Title level={3} className="!mb-2">
          Risks
        </Title>
        <Result
          status="error"
          title="Couldn't load the risk register."
          subTitle="The rest of this assessment is unaffected."
          extra={
            <Button type="primary" onClick={() => refetchRisks()}>
              Retry
            </Button>
          }
        />
      </div>
    );
  }

  return (
    <div>
      <Title level={3} className="!mb-2">
        Risks
      </Title>

      <Flex vertical gap="small" className="mb-3">
        <Flex gap="large" wrap="wrap">
          <div data-testid="risk-band-summary">
            <Text type="secondary" size="sm" className="block">
              Risk band
            </Text>
            {isLoading ? (
              <Skeleton.Input active size="small" />
            ) : (
              <RiskBandTag band={overallBand} />
            )}
          </div>
          <div>
            <Text type="secondary" size="sm" className="block">
              Highest risk
            </Text>
            {isLoading ? (
              <Skeleton.Input active size="small" />
            ) : (
              <Text data-testid="highest-risk-summary">
                {highestRisk
                  ? `${RISK_CATEGORY_LABELS[highestRisk.category as RiskCategory] ?? highestRisk.category} — ${highestRisk.description}`
                  : "No risks recorded yet."}
              </Text>
            )}
          </div>
          <div>
            <Text type="secondary" size="sm" className="block">
              Prior consultation
            </Text>
            <ConsultationStatus
              isLoading={isLoadingOdpc}
              isError={isOdpcError}
              required={odpcFinding?.required}
              onRetry={refetchOdpc}
            />
          </div>
        </Flex>

        {/* DESIGN.md: "a number nobody understands is a number nobody
            trusts" — the arithmetic, stated once, under the summary strip. */}
        <Text type="secondary" size="sm">
          Each risk scores likelihood × severity, from 1 to 25. The assessment
          takes the band of its highest risk, never an average — one severe risk
          is not cancelled out by several minor ones.
        </Text>

        {!isLoadingOdpc && !isOdpcError && odpcFinding?.required && (
          <PriorConsultationBanner reason={odpcFinding.reason} />
        )}
      </Flex>

      <Flex justify="end" className="mb-2">
        <Restrict scopes={[ScopeRegistryEnum.PRIVACYCARE_RISK_CREATE]}>
          <Button
            type="primary"
            icon={<Icons.Add />}
            data-testid="add-risk"
            onClick={() => setIsAddOpen(true)}
          >
            Add risk
          </Button>
        </Restrict>
      </Flex>

      <Table<RiskResponse>
        rowKey="id"
        columns={columns}
        dataSource={isLoading ? SKELETON_ROWS : risks}
        pagination={false}
        locale={{
          emptyText:
            "No risks recorded. The risk band stays Low until a risk is added.",
        }}
      />

      <AddRiskModal
        open={isAddOpen}
        onClose={() => setIsAddOpen(false)}
        assessmentId={assessmentId}
        currentOverallBand={overallBand}
      />

      {riskToRemove && (
        <RemoveRiskModal
          open
          onClose={() => setRiskToRemove(null)}
          assessmentId={assessmentId}
          risk={riskToRemove}
          remainingRisks={risks.filter((r) => r.id !== riskToRemove.id)}
        />
      )}
    </div>
  );
};
