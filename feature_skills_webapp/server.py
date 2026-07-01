def main() -> None:
    import uvicorn

    from feature_skills_webapp import config
    from feature_skills_webapp.web.app import create_app

    app = create_app(config.db_path())
    uvicorn.run(app, host=config.host(), port=config.port(), log_level="info")
