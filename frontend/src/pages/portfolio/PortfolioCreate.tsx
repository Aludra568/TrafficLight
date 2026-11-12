import { CloseOutlined, CloudDownloadOutlined, FileOutlined, InboxOutlined } from "@ant-design/icons";
import { Alert, App, Button, Card, Input, List, Radio, Space, Tooltip, Typography, Upload } from "antd";
import { useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";

import { ApiError } from "../../api/client";
import { generateUuid, useCreatePortfolio } from "../../api/portfolios";
import { brand } from "../../theme";
import { formatBytes, LIMITS, parseEisNumbers, validatePortfolioFiles } from "./portfolioUtils";
import { EisSources } from "./EisSources";

const ACCEPT = ".zip,.rar,.7z,.pdf,.doc,.docx,.rtf,.odt,.xls,.xlsx,.ods,.txt,.xml,.jpg,.jpeg,.png,.tif,.tiff";

export function PortfolioCreate() {
  const { message } = App.useApp();
  const navigate = useNavigate();
  const [title, setTitle] = useState("Портфель закупок");
  const [mode, setMode] = useState<"files" | "eis">("files");
  const [files, setFiles] = useState<File[]>([]);
  const [eisText, setEisText] = useState("");
  const previousRequest = useRef<{ title: string; mode: string; files: File[]; text: string; key: string } | null>(null);
  const createPortfolio = useCreatePortfolio();

  const fileValidation = useMemo(() => validatePortfolioFiles(files), [files]);
  const eisValidation = useMemo(() => (eisText.trim() ? parseEisNumbers(eisText) : null), [eisText]);

  const canLaunch = useMemo(() => {
    if (!title.trim() || title.length > 200) return false;
    if (mode === "files") return fileValidation.valid;
    return Boolean(eisValidation && eisValidation.valid.length > 0
      && eisValidation.valid.length <= LIMITS.maxFiles && eisValidation.invalid.length === 0);
  }, [title, mode, fileValidation, eisValidation]);

  const handleLaunch = () => {
    const trimmedTitle = title.trim();
    if (!trimmedTitle) return;
    const previous = previousRequest.current;
    if (!previous || previous.title !== trimmedTitle || previous.mode !== mode
      || previous.files !== files || previous.text !== eisText) {
      previousRequest.current = { title: trimmedTitle, mode, files, text: eisText, key: generateUuid() };
    }
    const requestKey = previousRequest.current!.key;

    createPortfolio.mutate(
      {
        title: trimmedTitle,
        requestKey,
        files: mode === "files" ? files : undefined,
        numbers: mode === "eis" && eisValidation ? eisValidation.valid : undefined,
      },
      {
        onSuccess: (res) => {
          if (res.dispatch_error) {
            message.warning(`Портфель создан с предупреждением: ${res.dispatch_error}`);
          } else {
            message.success("Портфель создан, проверки запущены");
          }
          navigate(`/portfolios/${res.id}`);
        },
        onError: (err) => {
          message.error(err instanceof ApiError ? err.message : "Не удалось создать портфель");
        },
      },
    );
  };

  return (
    <Card styles={{ body: { padding: 24 } }} style={{ maxWidth: 880, margin: "0 auto" }}>
      <Space direction="vertical" size={20} style={{ width: "100%" }}>
        <div>
          <Typography.Text strong style={{ display: "block", marginBottom: 6 }}>
            Название портфеля
          </Typography.Text>
          <Input
            aria-label="Название портфеля"
            size="large"
            value={title}
            maxLength={200}
            showCount
            placeholder="Например: Закупки компьютерного оборудования (ноябрь)"
            onChange={(e) => setTitle(e.target.value)}
          />
        </div>

        <div>
          <Typography.Text strong style={{ display: "block", marginBottom: 6 }}>
            Режим добавления закупок
          </Typography.Text>
          <Radio.Group value={mode} onChange={(e) => setMode(e.target.value)} buttonStyle="solid">
            <Radio.Button value="files">Файлы закупок</Radio.Button>
            <Radio.Button value="eis">Номера ЕИС</Radio.Button>
          </Radio.Group>
        </div>

        {mode === "files" ? (
          <div>
            <Upload.Dragger
              multiple
              accept={ACCEPT}
              showUploadList={false}
              beforeUpload={(file, list) => {
                if (file === list[0]) {
                  setFiles((prev) => [...prev, ...list]);
                }
                return false;
              }}
              style={{ background: brand.subtle, borderRadius: 8 }}
            >
              <p style={{ fontSize: 36, color: brand.slate, margin: "8px 0" }}>
                <InboxOutlined />
              </p>
              <p style={{ fontSize: 16, fontWeight: 600, margin: 0 }}>
                Перетащите файлы закупок сюда (каждый файл = одна закупка)
              </p>
              <p className="muted" style={{ margin: "4px 0 0", fontSize: 13 }}>
                Архивы или документы · До {LIMITS.maxFiles} файлов, до {LIMITS.maxFileMb} МБ на файл, суммарно до {LIMITS.maxTotalMb} МБ
              </p>
            </Upload.Dragger>

            {files.length > 0 && (
              <List
                size="small"
                style={{ marginTop: 14 }}
                header={
                  <Space style={{ justifyContent: "space-between", width: "100%" }}>
                    <Typography.Text strong>
                      Выбрано: {files.length} (суммарно {formatBytes(files.reduce((s, f) => s + f.size, 0))})
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
                      <Tooltip title="Убрать файл" key="del">
                        <Button
                          type="text"
                          size="small"
                          icon={<CloseOutlined />}
                          onClick={() => setFiles((prev) => prev.filter((f) => f !== file))}
                        />
                      </Tooltip>,
                    ]}
                  >
                    <Space>
                      <FileOutlined style={{ color: brand.gray }} />
                      <span>{file.name}</span>
                      <span className="muted num">{formatBytes(file.size)}</span>
                    </Space>
                  </List.Item>
                )}
              />
            )}

            {!fileValidation.valid && files.length > 0 && (
              <Alert type="error" message={fileValidation.error} showIcon style={{ marginTop: 12 }} />
            )}
          </div>
        ) : (
          <EisSources text={eisText} onChange={setEisText} validation={eisValidation} />
        )}

        <Button
          type="primary"
          size="large"
          icon={<CloudDownloadOutlined />}
          disabled={!canLaunch}
          loading={createPortfolio.isPending}
          onClick={handleLaunch}
          block
        >
          Запустить проверку портфеля
        </Button>
      </Space>
    </Card>
  );
}
