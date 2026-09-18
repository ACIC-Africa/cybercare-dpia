import { Result } from "fidesui";
import type { NextPage } from "next";

import { useFeatures } from "~/features/common/features";
import Layout from "~/features/common/Layout";
import { PRIVACY_ASSESSMENTS_ROUTE } from "~/features/common/nav/routes";
import PageHeader from "~/features/common/PageHeader";
import { DiscoveryScreen } from "~/features/privacy-assessments";

/**
 * PrivacyCare — `/privacy-assessments/discovery`, Screen 4 of
 * docs/design/privacycare-screens/DESIGN.md: finds personal data nobody
 * wrote down. Reachable only through the "Discovery" entry this task adds
 * to the "Privacy assessments" nav group (`~/features/common/nav/nav-config.tsx`)
 * — a page with no nav entry is unreachable in this product, the same
 * caution screening.tsx's and ropa.tsx's own header comments carry for
 * their sibling screens: `ProtectedRoute` exempts only `/` and `/login`
 * from a redux-persist rehydration race, so a typed URL or a page refresh
 * on any other route bounces to login.
 */
const PrivacyAssessmentDiscoveryPage: NextPage = () => {
  const { flags } = useFeatures();

  if (!flags?.privacyAssessments) {
    return (
      <Layout title="Discovery">
        <Result
          status="error"
          title="Feature not available"
          subTitle="This feature is currently behind a feature flag and is not enabled."
        />
      </Layout>
    );
  }

  return (
    <Layout title="Discovery">
      <PageHeader
        heading="Privacy assessments"
        breadcrumbItems={[
          { title: "Privacy assessments", href: PRIVACY_ASSESSMENTS_ROUTE },
          { title: "Discovery" },
        ]}
        isSticky
      />
      <div className="py-6">
        <DiscoveryScreen />
      </div>
    </Layout>
  );
};

export default PrivacyAssessmentDiscoveryPage;
