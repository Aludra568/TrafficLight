import { Button, Result } from "antd";
import { Component, type ErrorInfo, type ReactNode } from "react";

interface State {
  failed: boolean;
}

/**
 * Ошибка в одном экране не должна гасить всё приложение: вместо белой страницы — понятное
 * сообщение и выход. Шапка и меню остаются рабочими (граница стоит вокруг содержимого страницы
 * и сбрасывается при переходе на другой адрес).
 */
export class ErrorBoundary extends Component<{ children: ReactNode }, State> {
  state: State = { failed: false };

  static getDerivedStateFromError(): State {
    return { failed: true };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error("Сбой интерфейса", error, info.componentStack);
  }

  render() {
    if (!this.state.failed) return this.props.children;
    return (
      <Result
        status="warning"
        title="Экран не удалось показать"
        subTitle="Данные проверки не пострадали. Обновите страницу; если ошибка повторится — откройте другой раздел и сообщите администратору."
        extra={[
          <Button key="reload" type="primary" onClick={() => window.location.reload()}>
            Обновить страницу
          </Button>,
          <Button key="home" href="/">
            На главную
          </Button>,
        ]}
      />
    );
  }
}
