import { Button, Result } from "antd";
import { Link } from "react-router-dom";

export function NotFoundPage() {
  return (
    <div className="page">
      <Result
        status="404"
        title="Страница не найдена"
        subTitle="Возможно, ссылка устарела или проверка удалена."
        extra={
          <Link to="/checks">
            <Button type="primary">К списку проверок</Button>
          </Link>
        }
      />
    </div>
  );
}
