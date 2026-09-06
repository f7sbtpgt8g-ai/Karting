import { Suspense } from "react";
import LoginForm from "./LoginForm";

// The form reads `?next=` to return you where the middleware interrupted you,
// and useSearchParams() opts a route out of static prerendering unless it sits
// behind a Suspense boundary -- which is why the page is split in two.
export default function LoginPage() {
  return (
    <Suspense fallback={null}>
      <LoginForm />
    </Suspense>
  );
}
