import classNames from "classnames";
import {
  Avatar,
  Button,
  Card,
  CUSTOM_TAG_COLOR,
  Divider,
  Flex,
  Icons,
  Paragraph,
  Progress,
  Spin,
  Tag,
  TagList,
  Text,
  Typography,
} from "fidesui";

import useTaxonomies from "~/features/common/hooks/useTaxonomies";
import { RouterLink } from "~/features/common/nav/RouterLink";
import { PRIVACY_ASSESSMENTS_ROUTE } from "~/features/common/nav/routes";
import { formatDate } from "~/features/common/utils";

import styles from "./AssessmentCard.module.scss";
import { ASSESSMENT_STATUS_LABELS } from "./constants";
import { RISK_BAND_LABELS, RISK_BAND_TAG_COLORS } from "./risk.constants";
import { RiskBand } from "./risk.types";
import { AssessmentStatus, PrivacyAssessmentResponse } from "./types";

const { Title } = Typography;

type TextType = React.ComponentProps<typeof Typography.Text>["type"];

function getStatusTextType(status: AssessmentStatus): TextType {
  if (status === AssessmentStatus.COMPLETED) {
    return "success";
  }
  if (status === AssessmentStatus.OUTDATED) {
    return "danger";
  }
  return "secondary";
}

interface AssessmentCardProps {
  assessment: PrivacyAssessmentResponse;
  onClick: () => void;
}

export const AssessmentCard = ({
  assessment,
  onClick,
}: AssessmentCardProps) => {
  const { getDataCategoryDisplayName } = useTaxonomies();

  // Do not assume defaults for missing values; show "N/A" when absent
  //
  // Fix wave (Screen 2 review), finding 1. This USED to read
  // assessment.risk_level — Ethyca's own three-value projection, which
  // stores CRITICAL as "high" (risk/banding.py's projected_risk_level).
  // The assessment detail page one click away
  // (RiskRegisterSection.tsx) reads the TRUE four-value band from the risk
  // API, so a critical assessment could say "High" here and "Critical"
  // there. risk_band (assessments.py's _risk_bands_by_assessment) is the
  // same true band, computed the same way (risk/banding.py's band(), via
  // overall_band's "highest risk, never an average" rule) — reading it here
  // means the two screens can no longer disagree. Cast through
  // `as RiskBand`, the same discipline RiskRegisterSection.tsx and
  // RemoveRiskModal.tsx already apply to a server-sourced category/band
  // string: the wire value is always one of the four bands, there just is
  // no generated enum type on this field to prove it statically.
  const riskBand = (assessment.risk_band as RiskBand | null | undefined) ?? null;
  const status = assessment.status ?? null;
  const completeness = assessment.completeness ?? 0;

  const statusLabel = status ? ASSESSMENT_STATUS_LABELS[status] : "N/A";
  const isGenerating = status === AssessmentStatus.GENERATING;
  const isComplete = status === AssessmentStatus.COMPLETED;
  const completionDate =
    isComplete && assessment.updated_at
      ? `Completed on ${formatDate(assessment.updated_at, { showTime: false })}`
      : statusLabel;

  const titleText = assessment.template_name ?? assessment.name;

  return (
    <Card
      className={classNames(styles.cardWrapper, {
        [styles.cardComplete]: isComplete,
      })}
    >
      <Flex vertical gap="small" justify="space-between" className="flex-1">
        <div>
          <Title level={3} className={`!mb-1 ${styles.titleLink}`}>
            {isGenerating ? (
              titleText
            ) : (
              <RouterLink
                unstyled
                href={`${PRIVACY_ASSESSMENTS_ROUTE}/${assessment.id}`}
              >
                {titleText}
              </RouterLink>
            )}
          </Title>
          {assessment.system_name && (
            <Text type="secondary" size="sm" className="block">
              {assessment.system_name}
            </Text>
          )}
          <div className={styles.textWithTags}>
            {(assessment.data_categories ?? []).length > 0 ? (
              <TagList
                tags={(assessment.data_categories ?? []).map((key) => ({
                  value: key,
                  label: getDataCategoryDisplayName(key),
                }))}
                maxTags={1}
                expandable
              />
            ) : (
              <Tag>0 data categories</Tag>
            )}
          </div>
          {riskBand && (
            <div>
              <Tag
                color={RISK_BAND_TAG_COLORS[riskBand] ?? CUSTOM_TAG_COLOR.DEFAULT}
              >
                {`${RISK_BAND_LABELS[riskBand]} risk`}
              </Tag>
            </div>
          )}
        </div>
        <div>
          <Divider className="my-3" />
          <div>
            {isGenerating && (
              <Flex align="center" justify="center" gap="small">
                <div>
                  <Spin size="small" />
                </div>
                <Text type="secondary" size="sm">
                  Generating this assessment
                </Text>
              </Flex>
            )}
            {isComplete && (
              <Flex
                justify="space-between"
                align="center"
                className={styles.completeContainer}
              >
                <Flex align="center" gap="medium">
                  <Avatar
                    shape="circle"
                    size={28}
                    icon={<Icons.Checkmark size={14} />}
                    style={{ backgroundColor: "var(--fidesui-color-success)" }}
                  />
                  <div>
                    <Text strong type="success" size="sm">
                      Completed
                    </Text>
                    <Paragraph type="secondary" size="sm">
                      {completionDate}
                    </Paragraph>
                  </div>
                </Flex>
                <Button type="link" className="p-0" onClick={onClick}>
                  View
                </Button>
              </Flex>
            )}
            {!isGenerating && !isComplete && (
              <>
                <div>
                  <Text strong size="sm">
                    {Math.round(completeness)}%
                  </Text>
                  <Text type="secondary" size="sm">
                    {" "}
                    of questions answered
                  </Text>
                </div>
                <Progress
                  percent={completeness}
                  showInfo={false}
                  size="small"
                />
                <Flex justify="space-between" align="center" className="mt-1">
                  <Text type={getStatusTextType(status)} size="sm">
                    {statusLabel}
                  </Text>
                  <Button type="link" className="p-0" onClick={onClick}>
                    Resume
                  </Button>
                </Flex>
              </>
            )}
          </div>
        </div>
      </Flex>
    </Card>
  );
};
