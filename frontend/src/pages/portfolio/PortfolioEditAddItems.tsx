import { CloseOutlined, FileOutlined, InboxOutlined, PlusOutlined } from "@ant-design/icons";
import { Alert, App, Button, List, Modal, Radio, Space, Tooltip, Typography, Upload } from "antd";
import { useMemo, useRef, useState } from "react";

import { ApiError } from "../../api/client";
import { generateUuid } from "../../api/portfolios";
import { useAddPortfolioItems } from "../../api/portfolioEditing";
import { brand } from "../../theme";
import { EisSources } from "./EisSources";
import { LIMITS, formatBytes, parseEisNumbers, validatePortfolioFiles } from "./portfolioUtils";

const ACCEPT = ".zip,.rar,.7z,.pdf,.doc,.docx,.rtf,.odt,.xls,.xlsx,.ods,.txt,.xml,.jpg,.jpeg,.png,.tif,.tiff";

export interface PortfolioEditAddItemsProps {
  portfolioId: string;
  open: boolean;
  onClose: () => void;
}

export function PortfolioEditAddItems({ portfolioId, open, onClose }: PortfolioEditAddItemsProps) {
  const { message } = App.useApp();
  const [mode, setMode] = useState<"files" | "bundle" | "eis">("files");
  const [files, setFiles] = useState<File[]>([]);
  const [eisText, setEisText] = useState("");
  const previousRequest = useRef<{ mode: string; files: File[]; text: string; key: string } | null>(null);
  const addItemsMutation = useAddPortfolioItems(portfolioId);

  const fileValidation = useMemo(() => validatePortfolioFiles(files), [files]);
  const eisValidation = useMemo(() => (eisText.trim() ? parseEisNumbers(eisText) : null), [eisText]);

  const canSubmit = useMemo(() => {
    if (mode === "files" || mode === "bundle") return fileValidation.valid;
    return Boolean(eisValidation && eisValidation.valid.length > 0
      && eisValidation.valid.length <= LIMITS.maxFiles && eisValidation.invalid.length === 0);
  }, [mode, fileValidation, eisValidation]);

  const reset = () => {
    setFiles([]);
    setEisText("");
    setMode("files");
  };

  const handleClose = () => {
    reset();
    onClose();
  };

  const handleSubmit = () => {
    const prev = previousRequest.current;
    if (!prev || prev.mode !== mode || prev.files !== files || prev.text !== eisText) {
      previousRequest.current = { mode, files, text: eisText, key: generateUuid() };
    }
    const requestKey = previousRequest.current!.key;

    addItemsMutation.mutate(
      {
        requestKey,
        bundle: mode === "bundle",
        files: mode === "eis" ? undefined : files,
        numbers: mode === "eis" && eisValidation ? eisValidation.valid : undefined,
      },
      {
        onSuccess: (res) => {
          if (res.dispatch_error) {
            message.warning(`Закупки добавлены с предупреждением: ${res.dispatch_error}`);
          } else {
            message.success("Закупки добавлены в портфель");
          }
          handleClose();
        },
        onError: (err) => {
          if (err instanceof ApiError && err.status === 409) {
            message.error("Конфликт: портфель был изменён другим пользователем. Данные обновлены");
          } else {
            message.error(err instanceof ApiError ? err.message : "Не удалось добавить закупки");
          }
        },
      },
    );
  };

  return (
    <Modal
      title="Добавить закупки в портфель"
      open={open}
      onCancel={handleClose}
      width={720}
      footer={[
        <Button key="cancel" onClick={handleClose}>
          Отмена
        </Button>,
        <Button
          key="submit"
          type="primary"
          icon={<PlusOutlined />}
          disabled={!canSubmit}
          loading={addItemsMutation.isPending}
          onClick={handleSubmit}
        >
          Добавить закупки
        </Button>,
      ]}
    >
      <Space direction="vertical" size={16} style={{ width: "100%", marginTop: 12 }}>
        <Radio.Group value={mode} onChange={(e) => setMode(e.target.value)} buttonStyle="solid">
          <Radio.Button value="files">Файлы (по закупке на файл)</Radio.Button>
          <Radio.Button value="bundle">Одна закупка из документов</Radio.Button>
          <Radio.Button value="eis">Номера ЕИС</Radio.Button>
        </Radio.Group>

        {mode !== "eis" ? (
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
              <p style={{ fontSize: 28, color: brand.slate, margin: "4px 0" }}>
                <InboxOutlined />
              </p>
              <p style={{ fontSize: 14, fontWeight: 600, margin: 0 }}>
                {mode === "bundle"
                  ? "Перетащите файлы одной закупки (пакет документов)"
                  : "Перетащите файлы закупок (каждый файл = отдельная закупка)"}
              </p>
              <p className="muted" style={{ margin: "2px 0 0", fontSize: 12 }}>
                До {LIMITS.maxFiles} файлов, до {LIMITS.maxFileMb} МБ на файл, суммарно до {LIMITS.maxTotalMb} МБ
              </p>
            </Upload.Dragger>

            {files.length > 0 && (
              <List
                size="small"
                style={{ marginTop: 12 }}
                header={
                  <Space style={{ justifyContent: "space-between", width: "100%" }}>
                    <Typography.Text strong>
                      Выбрано: {files.length} ({formatBytes(files.reduce((s, f) => s + f.size, 0))})
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
              <Alert type="error" message={fileValidation.error} showIcon style={{ marginTop: 10 }} />
            )}
          </div>
        ) : (
          <EisSources text={eisText} onChange={setEisText} validation={eisValidation} />
        )}
      </Space>
    </Modal>
  );
}
