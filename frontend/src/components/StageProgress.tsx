import {
  CheckCircleFilled,
  CloseCircleFilled,
  LoadingOutlined,
  MinusCircleOutlined,
} from "@ant-design/icons";
import { Progress } from "antd";

import type { StageOut } from "../api/types";
import { brand, lights } from "../theme";

function StageIcon({ status }: { status: StageOut["status"] }) {
  const style = { fontSize: 18 };
  if (status === "done") return <CheckCircleFilled style={{ ...style, color: lights.green.solid }} />;
  if (status === "running") return <LoadingOutlined style={{ ...style, color: brand.navy }} />;
  if (status === "failed") return <CloseCircleFilled style={{ ...style, color: lights.red.solid }} />;
  if (status === "skipped") return <MinusCircleOutlined style={{ ...style, color: brand.gray }} />;
  return (
    <span
      aria-hidden
      style={{
        display: "inline-block",
        width: 16,
        height: 16,
        margin: 1,
        borderRadius: "50%",
        border: `2px solid ${brand.borderStrong}`,
      }}
    />
  );
}

/** Этапы проверки сверху вниз: что сделано, что идёт сейчас (с процентом), что впереди. */
export function StageProgress({ stages }: { stages: StageOut[] }) {
  return (
    <ol style={{ listStyle: "none", margin: 0, padding: 0 }} aria-label="Этапы проверки">
      {stages.map((stage, index) => {
        const active = stage.status === "running";
        const last = index === stages.length - 1;
        return (
          <li key={stage.stage} style={{ display: "flex", gap: 14 }}>
            <div style={{ display: "flex", flexDirection: "column", alignItems: "center" }}>
              <StageIcon status={stage.status} />
              {!last && (
                <span
                  aria-hidden
                  style={{
                    flex: 1,
                    width: 2,
                    minHeight: 18,
                    margin: "4px 0",
                    background: stage.status === "done" ? lights.green.border : brand.border,
                  }}
                />
              )}
            </div>
            <div style={{ paddingBottom: last ? 0 : 14, flex: 1 }}>
              <div
                style={{
                  fontWeight: active ? 600 : 500,
                  color: stage.status === "pending" ? brand.textTertiary : brand.navy,
                }}
              >
                {stage.title}
                {stage.status === "skipped" && <span className="muted"> — не требуется</span>}
              </div>
              {stage.detail && (
                <div className="muted" style={{ fontSize: 13, marginTop: 2 }}>
                  {stage.detail}
                </div>
              )}
              {active && stage.progress > 0 && stage.progress < 100 && (
                <Progress
                  percent={stage.progress}
                  size="small"
                  strokeColor={brand.navy}
                  style={{ maxWidth: 320, marginTop: 4 }}
                />
              )}
            </div>
          </li>
        );
      })}
    </ol>
  );
}
