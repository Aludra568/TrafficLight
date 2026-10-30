import { ReloadOutlined } from "@ant-design/icons";
import { App, Button, Card, Result, Skeleton, Typography } from "antd";
import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { ApiError } from "../api/client";
import { useCheck, useCheckEvents, useRerun } from "../api/hooks";
import { ACTIVE_STATUSES, type CheckOut } from "../api/types";
import { StageProgress } from "../components/StageProgress";
import { StatusTag } from "../components/StatusTag";
import { duration } from "../lib/format";
import { ReportView } from "./report/ReportView";

function Elapsed({ since }: { since: string | null }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);
  if (!since) return null;
  return <span className="num">{duration((now - new Date(since).getTime()) / 1000)}</span>;
}

function Progress({ check }: { check: CheckOut }) {
  return (
    <div className="page" style={{ maxWidth: 720 }}>
      <h1 className="page-title">{check.package.title}</h1>
      <p className="page-subtitle">
        <StatusTag status={check.status} /> · идёт <Elapsed since={check.started_at ?? check.created_at} />
      </p>
      <Card styles={{ body: { padding: 28 } }}>
        <StageProgress stages={check.stages} />
        <Typography.Paragraph type="secondary" style={{ margin: "20px 0 0" }}>
          Обычно проверка занимает 1–2 минуты. Можно закрыть страницу — проверка продолжится, результат
          будет в истории.
        </Typography.Paragraph>
      </Card>
    </div>
  );
}

function Failed({ check }: { check: CheckOut }) {
  const { message } = App.useApp();
  const navigate = useNavigate();
  const rerun = useRerun();
  return (
    <div className="page" style={{ maxWidth: 720 }}>
      <Result
        status="error"
        title="Проверка не удалась"
        subTitle={check.error ?? "Внутренняя ошибка сервиса"}
        extra={[
          <Button
            key="rerun"
            type="primary"
            icon={<ReloadOutlined />}
            loading={rerun.isPending}
            onClick={() =>
              rerun.mutate(check.id, {
                onSuccess: (r) => navigate(`/checks/${r.check_id}`),
                onError: (e) => message.error(e instanceof ApiError ? e.message : "Не удалось"),
              })
            }
          >
            Запустить ещё раз
          </Button>,
          <Link key="new" to="/new">
            <Button>Новая проверка</Button>
          </Link>,
        ]}
      />
      <Card size="small" title="Этапы">
        <StageProgress stages={check.stages} />
      </Card>
    </div>
  );
}

export function CheckPage() {
  const { id = "" } = useParams();
  const check = useCheck(id);
  const active = Boolean(check.data && ACTIVE_STATUSES.includes(check.data.status));
  useCheckEvents(id, active);

  if (check.isLoading) {
    return (
      <div className="page">
        <Skeleton active paragraph={{ rows: 8 }} />
      </div>
    );
  }
  if (check.error || !check.data) {
    const notFound = check.error instanceof ApiError && check.error.status === 404;
    return (
      <div className="page">
        <Result
          status={notFound ? "404" : "error"}
          title={notFound ? "Проверка не найдена" : "Не удалось загрузить проверку"}
          subTitle={check.error instanceof ApiError ? check.error.message : undefined}
          extra={
            <Link to="/checks">
              <Button>К списку проверок</Button>
            </Link>
          }
        />
      </div>
    );
  }
  if (active) return <Progress check={check.data} />;
  if (check.data.status === "failed") return <Failed check={check.data} />;
  return <ReportView check={check.data} />;
}
