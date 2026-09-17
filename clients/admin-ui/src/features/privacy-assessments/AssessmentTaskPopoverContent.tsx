import { Descriptions, Flex, Progress, Space, Spin, Tag, Text } from "fidesui";

import { useRelativeTime } from "~/features/common/hooks/useRelativeTime";

import { AssessmentTaskResponse, TaskStatus } from "./types";
import { formatSystems, formatTypes } from "./utils";

// PrivacyCare (spec 2026-09-16 D-W2-7g): the generated OpenAPI contract
// (types/api/models/AssessmentTaskResponse.ts) now carries skipped_count,
// but this feature module's hand-authored AssessmentTaskResponse (./types)
// does not — that file is out of this task's edit budget. Extend it
// locally instead of widening the shared interface.
type TaskWithSkips = AssessmentTaskResponse & { skipped_count?: number };

// PrivacyCare (spec 2026-09-16 D-W2-7g): a screened-out activity is a
// decision ("no DPIA needed"), not an error or a skip-because-broken — say
// nothing when there is nothing to say (skipped_count is 0/undefined), and
// otherwise use the same "screened out" wording the backend and the spec
// use, never a synonym.
const screenedOutSuffix = (skippedCount?: number): string =>
  skippedCount ? `, ${skippedCount} screened out (no DPIA required)` : "";

interface AssessmentTaskPopoverContentProps {
  activeTask: TaskWithSkips | null;
  lastCompletedTask: TaskWithSkips | null;
  templateNamesMap?: Record<string, string>;
}

export const AssessmentTaskPopoverContent = ({
  activeTask,
  lastCompletedTask,
  templateNamesMap,
}: AssessmentTaskPopoverContentProps) => {
  const activeRelativeTime = useRelativeTime(
    activeTask?.created_at ? new Date(activeTask.created_at) : null,
  );
  const completedRelativeTime = useRelativeTime(
    lastCompletedTask?.updated_at
      ? new Date(lastCompletedTask.updated_at)
      : null,
  );

  if (activeTask) {
    return (
      <div className="w-80">
        <Descriptions column={1} size="small">
          <Descriptions.Item label="Status">
            <Flex align="center" gap="small">
              <div>
                <Spin size="small" />
              </div>
              <span>In progress</span>
            </Flex>
          </Descriptions.Item>
          <Descriptions.Item label="Progress">
            <Space orientation="vertical" size="small" className="w-full">
              <Text size="sm">
                {activeTask.completed_count} of {activeTask.total_count}{" "}
                assessments
                {/* PrivacyCare (spec 2026-09-16 D-W2-7g) */}
                {screenedOutSuffix(activeTask.skipped_count)}
              </Text>
              <Progress
                percent={Math.round(activeTask.progress)}
                size="small"
              />
            </Space>
          </Descriptions.Item>
          <Descriptions.Item label="Type">
            {formatTypes(activeTask.assessment_types, templateNamesMap)}
          </Descriptions.Item>
          <Descriptions.Item label="Systems">
            {formatSystems(activeTask)}
          </Descriptions.Item>
          <Descriptions.Item label="Started">
            {activeRelativeTime}
          </Descriptions.Item>
        </Descriptions>
      </div>
    );
  }

  if (!lastCompletedTask) {
    return (
      <Text type="secondary" size="sm">
        No evaluation history.
      </Text>
    );
  }

  const isError = lastCompletedTask.status === TaskStatus.ERROR;

  return (
    <div className="w-80">
      <Descriptions column={1} size="small">
        <Descriptions.Item label="Status">
          {isError ? (
            <Tag color="error">Failed</Tag>
          ) : (
            <Tag color="success">Completed</Tag>
          )}
        </Descriptions.Item>
        {/* PrivacyCare (spec 2026-09-16 D-W2-7g): the outcome — how many
            assessments the run produced, and how many activities the
            screening gate decided didn't need one. */}
        <Descriptions.Item label="Outcome">
          <Text size="sm">
            {lastCompletedTask.completed_count} assessments produced
            {screenedOutSuffix(lastCompletedTask.skipped_count)}
          </Text>
        </Descriptions.Item>
        <Descriptions.Item label="Type">
          {formatTypes(lastCompletedTask.assessment_types, templateNamesMap)}
        </Descriptions.Item>
        <Descriptions.Item label="Systems">
          {formatSystems(lastCompletedTask)}
        </Descriptions.Item>
        <Descriptions.Item label={isError ? "Failed" : "Completed"}>
          {completedRelativeTime}
        </Descriptions.Item>
      </Descriptions>
    </div>
  );
};
