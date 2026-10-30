import { App, Form, Input, Modal, Typography } from "antd";
import { useState } from "react";

import { api, ApiError } from "../../api/client";

/**
 * Отметка «проверено юристом». Кто проверил — обязательное поле: без автора отметка не имеет веса,
 * а в отчёте замечания по такому правилу перестают помечаться как справочные.
 */
export function VerifyRuleModal({
  code,
  open,
  onClose,
  onDone,
}: {
  code: string;
  open: boolean;
  onClose: () => void;
  onDone: () => void;
}) {
  const { message } = App.useApp();
  const [form] = Form.useForm<{ verified_by: string }>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async () => {
    let verified_by: string;
    try {
      ({ verified_by } = await form.validateFields());
    } catch {
      return; // ошибки уже показаны под полем
    }
    setBusy(true);
    setError(null);
    try {
      await api(`/api/rules/${code}/verify`, { method: "POST", json: { verified_by: verified_by.trim() } });
      message.success(`Правило ${code} отмечено как проверенное юристом`);
      form.resetFields();
      onDone();
      onClose();
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : "Не удалось сохранить отметку");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      open={open}
      title={`Правило ${code} проверено юристом`}
      okText="Отметить"
      cancelText="Отмена"
      confirmLoading={busy}
      onOk={submit}
      onCancel={() => {
        setError(null);
        onClose();
      }}
      destroyOnClose
    >
      <Typography.Paragraph type="secondary">
        После отметки замечания по правилу перестают помечаться как «правило — черновик». Норму
        закона стоит сверять заново раз в полгода.
      </Typography.Paragraph>
      <Form form={form} layout="vertical" requiredMark={false} onFinish={submit}>
        <Form.Item
          name="verified_by"
          label="Кто проверил"
          extra="ФИО и должность, например: Иванова А. С., юрист по закупкам"
          rules={[
            { required: true, whitespace: true, message: "Укажите, кто проверил правило" },
            { min: 3, message: "Слишком коротко" },
          ]}
        >
          <Input autoFocus maxLength={200} />
        </Form.Item>
        {error && <Typography.Text type="danger">{error}</Typography.Text>}
      </Form>
    </Modal>
  );
}
