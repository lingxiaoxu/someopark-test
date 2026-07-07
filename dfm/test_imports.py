def test_all_modules_importable():
    import DFM_code
    from DFM_code import (
        synthetic,
        preprocessing,
        data_init,
        diffusion,
        evaluation_init,
        experiments_init,
        model_init,
        numerical,
        portfolio_analysis,
        sampling,
        utils_init
    )
