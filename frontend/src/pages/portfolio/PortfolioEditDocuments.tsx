import { ArrowRightOutlined, CloseOutlined, DeleteOutlined, InboxOutlined, UndoOutlined } from "@ant-design/icons";
import { Alert, App, Button, Empty, List, Modal, Skeleton, Space, Tag, Typography, Upload } from "antd";
import { useRef, useState } from "react";
import { Link } from "react-router-dom";

import { ApiError } from "../../api/client";
import { generateUuid, type PortfolioItemOut } from "../../api/portfolios";
import { useItemDocuments, useUpdateItemDocuments } from "../../api/portfolioEditing";
import { brand } from "../../theme";
import { formatBytes } from "./portfolioUtils";

const ACCEPT = ".zip,.rar,.7z,.pdf,.doc,.docx,.rtf,.odt,.xls,.xlsx,.ods,.txt,.xml,.jpg,.jpeg,.png,.tif,.tiff";

export interface PortfolioEditDocumentsProps {
  portfolioId: string;
  item: PortfolioItemOut | null;
  open: boolean;
  onClose: () => void;
}

export function PortfolioEditDocuments({ portfolioId, item, open, onClose }: PortfolioEditDocumentsProps) {
  const { message } = App.useApp();
  const [removeIds, setRemoveIds] = useState<string[]>([]);
  const [newFiles, setNewFiles] = useState<File[]>([]);
  const prevReq = useRef<{ removeIds: string[]; files: File[]; key: string; version: number } | null>(null);

  const itemId = item?.id;
  const docsQuery = useItemDocuments(portfolioId, itemId, open);
  const updateMutation = useUpdateItemDocuments(portfolioId, itemId ?? "");

  const handleClose = () => {
    setRemoveIds([]);
    setNewFiles([]);
    onClose();
  };

  const docsData = docsQuery.data;
  const existingDocs = docsData?.documents ?? [];
  const activeCount = existingDocs.filter((d) => !removeIds.includes(d.id)).length + newFiles.length;
  const hasChanges = removeIds.length > 0 || newFiles.length > 0;
  const isLocked = updateMutation.isPending || Boolean(item?.editing) || item?.status === "running" || docsData?.editable === false;

  const handleSubmit = () => {
    if (!docsData || !itemId) return;
    const same = prevReq.current && prevReq.current.removeIds.join() === removeIds.join()
      && prevReq.current.files.length === newFiles.length
      && prevReq.current.files.every((file, index) => file === newFiles[index]);
    if (!same) prevReq.current = { removeIds: [...removeIds], files: [...newFiles], key: generateUuid(), version: docsData.item_version };

    updateMutation.mutate(
      {
        requestKey: prevReq.current!.key,
        version: prevReq.current!.version,
        removeIds,
        files: newFiles.length > 0 ? newFiles : undefined,
      },
      {
        onSuccess: (result) => {
          if (result.dispatch_error) message.warning(result.dispatch_error);
          else message.success("Запрос на обновление документов принят. Идёт перепроверка");
          handleClose();
        },
        onError: (err) => {
          if (err instanceof ApiError && err.status === 409) {
            prevReq.current = null;
            setRemoveIds([]);
            message.error("Конфликт версий: состав документов был изменён другим пользователем. Данные обновлены");
          } else {
            message.error(err instanceof ApiError ? err.message : "Не удалось обновить документы");
          }
        },
      },
    );
  };

  return (
    <Modal
      title={`Состав документов: ${item?.source_name ?? ""}`}
      open={open}
      onCancel={handleClose}
      width={680}
      footer={[
        <Button key="cancel" onClick={handleClose}>Закрыть</Button>,
        <Button
          key="save"
          type="primary"
          disabled={!hasChanges || activeCount === 0 || isLocked}
          loading={updateMutation.isPending}
          onClick={handleSubmit}
        >
          Сохранить и перепроверить
        </Button>,
      ]}
    >
      <Space direction="vertical" size={12} style={{ width: "100%", marginTop: 8 }}>
        {item?.check_id && (
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <span className="muted" style={{ fontSize: 13 }}>История: закупка уже была проверена</span>
            <Button type="link" size="small" icon={<ArrowRightOutlined />}>
              <Link to={`/checks/${item.check_id}`}>Отчёт проверки</Link>
            </Button>
          </div>
        )}
        {item?.editing && <Alert type="info" showIcon message="Идёт подготовка и перепроверка закупки в фоновом режиме" />}
        {item?.edit_error && <Alert type="error" showIcon message={`Ошибка подготовки: ${item.edit_error}`} />}

        {docsQuery.isLoading ? <Skeleton active paragraph={{ rows: 3 }} /> : docsQuery.isError ? (
          <Alert type="error" message="Не удалось загрузить документы" />
        ) : existingDocs.length === 0 ? (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="В закупке нет документов" />
        ) : (
          <List
            size="small"
            header={<Typography.Text strong>Текущие документы ({existingDocs.length})</Typography.Text>}
            dataSource={existingDocs}
            renderItem={(doc) => {
              const marked = removeIds.includes(doc.id);
              return (
                <List.Item
                  actions={[
                    marked ? (
                      <Button key="u" type="link" size="small" icon={<UndoOutlined />} onClick={() => setRemoveIds((p) => p.filter((x) => x !== doc.id))}>
                        Вернуть
                      </Button>
                    ) : (
                      <Button
                        key="d"
                        type="text"
                        size="small"
                        danger
                        icon={<DeleteOutlined />}
                        aria-label="Удалить документ"
                        disabled={isLocked}
                        onClick={() => setRemoveIds((p) => [...p, doc.id])}
                      />
                    ),
                  ]}
                >
                  <Space>
                    <span style={{ textDecoration: marked ? "line-through" : "none", color: marked ? brand.gray : "inherit" }}>
                      {doc.filename}
                    </span>
                    {marked ? <Tag color="error">К удалению</Tag> : <Tag>{doc.status}</Tag>}
                  </Space>
                </List.Item>
              );
            }}
          />
        )}

        {!isLocked && (
          <div style={{ marginTop: 8 }}>
            <Upload.Dragger
              multiple
              accept={ACCEPT}
              showUploadList={false}
              beforeUpload={(file, list) => {
                if (file === list[0]) setNewFiles((prev) => [...prev, ...list]);
                return false;
              }}
              style={{ background: brand.subtle, borderRadius: 8, padding: "8px 0" }}
            >
              <p style={{ fontSize: 24, color: brand.slate, margin: "2px 0" }}><InboxOutlined /></p>
              <p style={{ fontSize: 13, fontWeight: 600, margin: 0 }}>Добавить новые файлы к закупке</p>
            </Upload.Dragger>

            {newFiles.length > 0 && (
              <List
                size="small"
                style={{ marginTop: 8 }}
                dataSource={newFiles}
                renderItem={(file) => (
                  <List.Item
                    actions={[
                      <Button
                        key="x"
                        type="text"
                        size="small"
                        icon={<CloseOutlined />}
                        onClick={() => setNewFiles((p) => p.filter((f) => f !== file))}
                      />,
                    ]}
                  >
                    <Space>
                      <span>{file.name}</span>
                      <span className="muted num">{formatBytes(file.size)}</span>
                      <Tag color="green">Новый</Tag>
                    </Space>
                  </List.Item>
                )}
              />
            )}
          </div>
        )}
      </Space>
    </Modal>
  );
}
