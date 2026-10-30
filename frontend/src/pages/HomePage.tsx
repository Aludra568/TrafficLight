import { FileSearchOutlined, HistoryOutlined, PlusOutlined, SettingOutlined } from "@ant-design/icons";
import type { ReactNode } from "react";
import { Link } from "react-router-dom";

import { useChecks, useRules } from "../api/hooks";
import { useAuth } from "../auth/AuthProvider";
import { TrafficLight } from "../components/TrafficLight";
import { count } from "../lib/format";

/** Плитка раздела: вся карточка — одна ссылка, внутри нет других кнопок. */
function Tile({
  to,
  icon,
  title,
  primary = false,
  children,
}: {
  to: string;
  icon: ReactNode;
  title: string;
  primary?: boolean;
  children?: ReactNode;
}) {
  return (
    <Link to={to} className={`home-tile${primary ? " home-tile-primary" : ""}`}>
      <span className="home-tile-icon" aria-hidden>
        {icon}
      </span>
      <h2 className="home-tile-title">{title}</h2>
      {children && <div className="home-tile-body">{children}</div>}
    </Link>
  );
}

export function HomePage() {
  const { can } = useAuth();
  const methodologist = can("methodologist");
  const recent = useChecks({ page: 1, pageSize: 3 });
  const rules = useRules(methodologist);
  const total = recent.data?.total ?? 0;
  const ruleCount = methodologist ? (rules.data?.length ?? 0) : 0;

  return (
    <div className="page home">
      <h1 className="visually-hidden">Главная</h1>
      <div className={`home-tiles${methodologist ? " home-tiles-3" : ""}`}>
        <Tile to="/new" icon={<PlusOutlined />} title="Новая проверка" primary>
          Загрузить пакет документов закупки
        </Tile>
        <Tile to="/checks" icon={<HistoryOutlined />} title="Мои проверки">
          {recent.isLoading ? null : total === 0 ? (
            "Проверок пока нет"
          ) : (
            <>
              <div className="home-tile-count">{count(total, ["проверка", "проверки", "проверок"])}</div>
              <ul className="home-recent">
                {(recent.data?.items ?? []).map((item) => (
                  <li key={item.id}>
                    {item.light ? <TrafficLight light={item.light} iconOnly /> : <FileSearchOutlined />}
                    <span className="home-recent-title">{item.package_title}</span>
                  </li>
                ))}
              </ul>
            </>
          )}
        </Tile>
        {methodologist && (
          <Tile to="/rules" icon={<SettingOutlined />} title="Правила">
            {ruleCount > 0 && (
              <div className="home-tile-count">{count(ruleCount, ["правило", "правила", "правил"])}</div>
            )}
            Добавить или изменить без программиста
          </Tile>
        )}
      </div>
    </div>
  );
}
