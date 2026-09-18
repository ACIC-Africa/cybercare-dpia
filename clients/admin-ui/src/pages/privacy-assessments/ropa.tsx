import { Result } from "fidesui";
import type { NextPage } from "next";

import { useFeatures } from "~/features/common/features";
import Layout from "~/features/common/Layout";
import { PRIVACY_ASSESSMENTS_ROUTE } from "~/features/common/nav/routes";
import PageHeader from "~/features/common/PageHeader";
import { Ropa } from "~/features/privacy-assessments";

/**
 * PrivacyCare — `/privacy-assessments/ropa`, Screen 3 of
 * docs/design/privacycare-screens/DESIGN.md: the record of processing
 * activities (ROPA). Reachable only through the "Record of processing
 * activities" entry this task adds to the "Privacy assessments" nav group
 * (`~/features/common/nav/nav-config.tsx`) — a page with no nav entry is
 * unreachable in this product, the same caution screening.tsx's own header
 * comment carries for its sibling screen: `ProtectedRoute` exempts only
 * `/` and `/login` from a redux-persist rehydration race, so a typed URL
 * or a page refresh on any other route bounces to login.
 */
const PrivacyAssessmentRopaPage: NextPage = () => {
  const { flags } = useFeatures();

  if (!flags?.privacyAssessments) {
    return (
      <Layout title="Record of processing activities">
        <Result
          status="error"
          title="Feature not available"
          subTitle="This feature is currently behind a feature flag and is not enabled."
        />
      </Layout>
    );
  }

  return (
    <Layout title="Record of processing activities">
      <PageHeader
        heading="Privacy assessments"
        breadcrumbItems={[
          { title: "Privacy assessments", href: PRIVACY_ASSESSMENTS_ROUTE },
          { title: "Record of processing activities" },
        ]}
        isSticky
      />
      <div className="py-6">
        <Ropa />
      </div>
    </Layout>
  );
};

export default PrivacyAssessmentRopaPage;
