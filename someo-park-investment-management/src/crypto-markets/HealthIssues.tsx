import { ISSUE_GROUPS, issueKind, type HealthIssue } from "./healthSemantics";
import { publicText, Status } from "./primitives";

export default function HealthIssues({ issues }: { issues: HealthIssue[] }) {
  return (
    <div className="cm-issues">
      {ISSUE_GROUPS.map(({ kind, title }) => {
        const rows = issues.filter((issue) => issueKind(issue.code) === kind);
        if (rows.length === 0) return null;
        return (
          <div className="cm-issue-group" key={kind}>
            <h4>{title}（{rows.length}）</h4>
            {rows.map((issue, index) => (
              <div className={`cm-issue cm-issue-${kind}`} key={`${issue.source}:${issue.code}:${index}`}>
                <div className="cm-runtime-heading">
                  <b className="cm-break">{publicText(issue.source)}</b>
                  <Status value={issue.code} />
                </div>
                <p className="cm-prose">{publicText(issue.detail)}</p>
                {issue.code === "SOURCE_HASH_MISMATCH" && (
                  <p className="cm-note">需核对进程实际加载的版本；保留注册哈希与原始账本，不通过覆盖哈希消除差异。</p>
                )}
              </div>
            ))}
          </div>
        );
      })}
    </div>
  );
}
