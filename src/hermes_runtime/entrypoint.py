"""S6 default new-request profile; historical probes can opt into H5 explicitly."""


def resolve_profile(args):
    if getattr(args, "profile", None) == "scat-author":
        if any(
            getattr(args, name, False)
            for name in (
                "with_mcp",
                "with_skills",
                "with_skill_scripts",
                "with_workflows",
                "with_model_tasks",
                "background_job",
                "legacy_h5",
            )
        ):
            raise ValueError("author_profile_cannot_enable_business_tools")
        return args
    if args.legacy_h5:
        if args.with_skills or args.with_workflows or args.background_job:
            raise ValueError("legacy_h5_cannot_enable_skills")
        return args
    if not args.background_job and not args.with_skills:
        args.with_mcp = args.with_model_tasks = args.with_skills = args.with_workflows = True
    return args
