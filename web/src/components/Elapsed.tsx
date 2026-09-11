import { useEffect, useState } from "react";

/** Live seconds counter. Knowledge base retrieval can run for well over a minute,
 *  so an in-flight step needs to prove it is still working. */
export function Elapsed({ since }: { since: number }) {
  const [now, setNow] = useState(Date.now());

  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 200);
    return () => clearInterval(timer);
  }, []);

  return <span className="card-time">{((now - since) / 1000).toFixed(1)}s</span>;
}
