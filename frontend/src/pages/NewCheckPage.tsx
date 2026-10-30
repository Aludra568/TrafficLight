import {
  CloseOutlined,
  CloudDownloadOutlined,
  ExperimentOutlined,
  FileOutlined,
  FileProtectOutlined,
  InboxOutlined,
} from "@ant-design/icons";
import { App, Button, Card, Col, Divider, Input, List, Row, Select, Space, Tooltip, Typography, Upload } from "antd";
import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";

import { ApiError } from "../api/client";
import { useCreateCheck, useDemoCheck, useEisCheck, useRulesets, useSettings } from "../api/hooks";
import { count, megabytes } from "../lib/format";
import { brand } from "../theme";

const ACCEPT = ".zip,.rar,.7z,.pdf,.doc,.docx,.rtf,.odt,.xls,.xlsx,.ods,.txt,.xml,.jpg,.jpeg,.png,.tif,.tiff";

/** Реестровый номер ЕИС: 19 цифр — 44-ФЗ, 11 цифр — 223-ФЗ; пробелы и «№» не мешают. */
function eisNumber(raw: string): string | null {
  const digits = raw.replace(/[\s№#]/g, "");
  return /^(\d{19}|\d{11})$/.test(digits) ? digits : null;
}

const WHAT_WE_CHECK = [
  "Сроки оплаты, приёмки и поставки",
  "Неустойки, штрафы и односторонний отказ",
  "Обеспечение заявки, исполнения и гарантии",
  "Противоречия между ТЗ, контрактом и извещением",
  "Нацрежим, товарный знак, требования к участнику",
  "Скрытый текст и подмена букв в документах",
];

export function NewCheckPage() {
  const { message } = App.useApp();
  const navigate = useNavigate();
  const [files, setFiles] = useState<File[]>([]);
  const [ruleset, setRuleset] = useState<string | undefined>();
  const rulesets = useRulesets();
  const settings = useSettings();
  const create = useCreateCheck();
  const demo = useDemoCheck();
  const eis = useEisCheck();
  const [number, setNumber] = useState("");
  const validNumber = eisNumber(number);
  const eisEnabled = (settings.data as { eis_fetch?: boolean } | undefined)?.eis_fetch !== false;

  const limitMb = Number((settings.data?.limits as { max_upload_mb?: number } | undefined)?.max_upload_mb ?? 100);
  const total = useMemo(() => files.reduce((sum, f) => sum + f.size, 0), [files]);
  const tooLarge = total > limitMb * 1024 * 1024;

  const fail = (exc: unknown) =>
    message.error(exc instanceof ApiError ? exc.message : "Не удалось запустить проверку");

  const start = () =>
    create.mutate(
      { files, ruleset },
      { onSuccess: (r) => navigate(`/checks/${r.check_id}`), onError: fail },
    );

  const fromEis = () => {
    if (validNumber) eis.mutate(validNumber, { onSuccess: (r) => navigate(`/checks/${r.check_id}`), onError: fail });
  };

  const tryDemo = () =>
    demo.mutate(undefined, { onSuccess: (r) => navigate(`/checks/${r.check_id}`), onError: fail });

  return (
    <div className="page">
      <h1 className="page-title">Новая проверка</h1>
      <p className="page-subtitle">Сроки, оплата, ответственность и обеспечение — по документам закупки.</p>
      <Row gutter={[24, 24]}>
        <Col xs={24} lg={16}>
          <Card style={{ height: "100%" }} styles={{ body: { padding: 24 } }}>
            <Upload.Dragger
              multiple
              accept={ACCEPT}
              showUploadList={false}
              beforeUpload={(file, list) => {
                // все выбранные за раз файлы добавляются один раз, без дублей по имени
                if (file === list[0]) {
                  setFiles((prev) => {
                    const names = new Set(prev.map((f) => f.name));
                    return [...prev, ...list.filter((f) => !names.has(f.name))];
                  });
                }
                return false;
              }}
              style={{ background: brand.subtle, borderRadius: 8 }}
            >
              <p style={{ fontSize: 40, color: brand.slate, margin: "8px 0" }}>
                <InboxOutlined />
              </p>
              <p style={{ fontSize: 16, fontWeight: 600, margin: 0 }}>
                Перетащите архив или файлы сюда
              </p>
              <p className="muted" style={{ margin: "4px 0 0" }}>
                или нажмите, чтобы выбрать · ZIP, RAR, 7z, PDF, DOCX, DOC, RTF, XLSX, XML, сканы · до{" "}
                {limitMb} МБ
              </p>
            </Upload.Dragger>

            {files.length > 0 && (
              <List
                size="small"
                style={{ marginTop: 16 }}
                header={
                  <Space style={{ justifyContent: "space-between", width: "100%" }}>
                    <Typography.Text strong>
                      {count(files.length, ["файл", "файла", "файлов"])} · {megabytes(total)}
                    </Typography.Text>
                    <Button type="link" size="small" onClick={() => setFiles([])}>
                      Очистить
                    </Button>
                  </Space>
                }
                dataSource={files}
                renderItem={(file) => (
                  <List.Item
                    actions={[
                      <Tooltip title="Убрать файл" key="remove">
                        <Button
                          type="text"
                          size="small"
                          icon={<CloseOutlined />}
                          aria-label={`Убрать ${file.name}`}
                          onClick={() => setFiles((prev) => prev.filter((f) => f !== file))}
                        />
                      </Tooltip>,
                    ]}
                  >
                    <Space>
                      <FileOutlined style={{ color: brand.gray }} />
                      <span>{file.name}</span>
                      <span className="muted num">{megabytes(file.size)}</span>
                    </Space>
                  </List.Item>
                )}
              />
            )}
            {tooLarge && (
              <Typography.Paragraph type="danger" style={{ marginTop: 12, marginBottom: 0 }}>
                Файлы вместе больше {limitMb} МБ. Разделите пакет на части или уберите лишние файлы.
              </Typography.Paragraph>
            )}

            {/* набор правил — только когда есть из чего выбирать: один набор не требует выбора */}
            {(rulesets.data?.length ?? 0) > 1 && (
            <div style={{ display: "flex", alignItems: "center", gap: 12, marginTop: 20 }}>
              <label htmlFor="ruleset" className="muted" style={{ whiteSpace: "nowrap" }}>
                Набор правил
              </label>
              <Select
                id="ruleset"
                style={{ flex: 1, minWidth: 0 }}
                placeholder="44-ФЗ: риски поставщика"
                loading={rulesets.isLoading}
                value={ruleset ?? rulesets.data?.[0]?.code}
                onChange={setRuleset}
                options={(rulesets.data ?? []).map((r) => ({
                  value: r.code,
                  label: `${r.title} · v${r.version}`,
                }))}
              />
            </div>
            )}

            <div
              className="action-row"
              style={{
                display: "flex",
                flexWrap: "wrap",
                alignItems: "center",
                gap: 12,
                marginTop: 20,
              }}
            >
              <Button
                type="primary"
                size="large"
                icon={<FileProtectOutlined />}
                disabled={files.length === 0 || tooLarge}
                loading={create.isPending}
                onClick={start}
              >
                {files.length > 1 ? `Проверить ${count(files.length, ["файл", "файла", "файлов"])}` : "Проверить"}
              </Button>
              <Button size="large" icon={<ExperimentOutlined />} loading={demo.isPending} onClick={tryDemo}>
                Попробовать на примере
              </Button>
            </div>

            {eisEnabled && (
              <>
                <Divider plain style={{ margin: "24px 0 16px", color: brand.gray }}>
                  или по номеру закупки в ЕИС
                </Divider>
                <form
                  className="action-row"
                  style={{ display: "flex", flexWrap: "wrap", gap: 12 }}
                  onSubmit={(event) => {
                    event.preventDefault();
                    fromEis();
                  }}
                >
                  <Input
                    size="large"
                    inputMode="numeric"
                    maxLength={40}
                    aria-label="Реестровый номер закупки в ЕИС"
                    placeholder="Реестровый номер, например 0362100026126000023"
                    value={number}
                    onChange={(e) => setNumber(e.target.value)}
                    disabled={eis.isPending}
                    style={{ flex: "1 1 280px", minWidth: 0 }}
                  />
                  <Button
                    size="large"
                    htmlType="submit"
                    icon={<CloudDownloadOutlined />}
                    disabled={!validNumber}
                    loading={eis.isPending}
                  >
                    Загрузить из ЕИС
                  </Button>
                </form>
                <Typography.Paragraph type="secondary" style={{ margin: "8px 0 0", fontSize: 13 }}>
                  {eis.isPending
                    ? "Скачиваем извещение и документы с zakupki.gov.ru — обычно до минуты…"
                    : number && !validNumber
                      ? "Номер закупки — 19 цифр (44-ФЗ) или 11 цифр (223-ФЗ)"
                      : "Извещение и все документы закупки скачаются с zakupki.gov.ru, проверка начнётся сама."}
                </Typography.Paragraph>
              </>
            )}
          </Card>
        </Col>

        <Col xs={24} lg={8}>
          <Card title="Что проверяем" size="small" style={{ height: "100%" }} styles={{ body: { paddingTop: 8 } }}>
            <ul style={{ margin: 0, paddingLeft: 18, lineHeight: 1.9 }}>
              {WHAT_WE_CHECK.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
            <Typography.Paragraph type="secondary" style={{ margin: "12px 0 0", fontSize: 13 }}>
              Каждое замечание — с точной цитатой и местом в документе. Что не удалось проверить,
              показываем честно.
            </Typography.Paragraph>
          </Card>
        </Col>
      </Row>

    </div>
  );
}
