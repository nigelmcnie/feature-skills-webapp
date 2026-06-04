def main() -> None:
    import uvicorn

    from feature_skills_webapp import config
    from feature_skills_webapp.web.app import create_app

    app = create_app(config.db_path(), config.docs_root())
    uvicorn.run(app, host="127.0.0.1", port=config.port(), log_level="info")
