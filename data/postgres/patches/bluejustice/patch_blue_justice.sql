-- This transforms the old 'Experiences' table into the 'Blue Justice' table

-- 1. Add example column
ALTER TABLE public.ssf_experience
    ADD example_column VARCHAR;