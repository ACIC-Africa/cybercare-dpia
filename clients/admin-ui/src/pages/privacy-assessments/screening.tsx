import { Result } from "fidesui";
import type { NextPage } from "next";

import { useFeatures } from "~/features/common/features";
import Layout from "~/features/common/Layout";
import { PRIVACY_ASSESSMENTS_ROUTE } from "~/features/common/nav/routes";
import PageHeader from "~/features/common/PageHeader";
import { Screening } from "~/features/privacy-assessments";

/**
 * PrivacyCare (spec 2026-09-16 D-W2-7g): `/privacy-assessments/screening`.
 * Reachable only through the "Screening" entry this task adds to the
 * "Privacy assessments" nav group (`~/features/common/nav/nav-config.tsx`) —
 * a page with no nav entry is unreachable in this product: `ProtectedRoute`
 * exempts only `/` and `/login` from a redux-persist rehydration race, so a
 * typed URL or a page refresh on any other route bounces to login.
 */
const PrivacyAssessmentScreeningPage: NextPage = () => {
  const { flags } = useFeatures();

  if (!flags?.privacyAssessments) {
    return (
      <Layout title="Screening">
        <Result
          status="error"
          title="Feature not available"
          subTitle="This feature is currently behind a feature flag and is not enabled."
        />
      </Layout>
    );
  }

  return (
    <Layout title="Screening">
      <PageHeader
        heading="Privacy assessments"
        breadcrumbItems={[
          { title: "Privacy assessments", href: PRIVACY_ASSESSMENTS_ROUTE },
          { title: "Screening" },
        ]}
        isSticky
      />
      <div className="py-6">
        <Screening />
      </div>
    </Layout>
  );
};

export default PrivacyAssessmentScreeningPage;
