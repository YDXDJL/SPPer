import Link from "next/link";

interface SiteHeaderProps {
  active: "monitor" | "dev";
  connected: boolean;
}

export default function SiteHeader({ active, connected }: SiteHeaderProps) {
  return (
    <header className="site-header">
      <Link className="brand" href="/">
        <span className="brand-mark">冠</span>
        <span>
          <strong>冠影守望者</strong>
          <small>泳池智能安全防护系统</small>
        </span>
      </Link>
      <nav aria-label="主导航">
        <Link className={active === "monitor" ? "active" : ""} href="/">
          运行监控
        </Link>
        <Link className={active === "dev" ? "active" : ""} href="/dev/">
          开发测试
        </Link>
      </nav>
      <div className={`server-state ${connected ? "online" : "offline"}`}>
        <span />
        {connected ? "服务器已连接" : "正在重连"}
      </div>
    </header>
  );
}
