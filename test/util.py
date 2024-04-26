from pathlib import Path
import re

import pandas as pd
import pysam


INPUT_TYPES_EXTENSIONS = {
    "fastq": ["fastq", "fastq.gz", "fq", "fq.gz"],
    "bam": ["bam", "ubam"],
}


def check_input_type(input_type):
    if input_type not in INPUT_TYPES_EXTENSIONS:
        raise ValueError(
            f"`input_type` needs to be one of {INPUT_TYPES_EXTENSIONS.keys()}."
        )


def is_target_file(file, input_type):
    """Check if `file` is of `input_type`."""
    if not file.is_file():
        return False
    exts = INPUT_TYPES_EXTENSIONS[input_type]
    return any(map(lambda ext: file.name.endswith(ext), exts))


def get_target_files(path, input_type):
    """Return a list of target files in the directory."""
    return list(filter(lambda file: is_target_file(file, input_type), path.iterdir()))


def create_preliminary_meta(path, input_type, chunk_size, bam_headers_in_fastq):
    """Create a dict of sequence IDs / names and run_ids.

    :param path: can be a single target file, a list of target files, or a directory
        containing target files.
    :param input_type: can either be "fastq" or "bam"
    :param bam_headers_in_fastq: the fastq was generated by `samtools fastq -T "*"`

    For FASTQ files, the run IDs are by default assumed to be present in the header
    lines in the format `runid=...`. When setting `bam_headers_in_fastq`, the function
    instead searches for `RD:Z:...`.
    """
    check_input_type(input_type)
    names = []
    run_ids = set()
    if isinstance(path, list):
        target_files = path
    elif path.is_dir():
        target_files = get_target_files(path, input_type)
    elif path.is_file():
        target_files = [path]
    else:
        raise ValueError(
            f"`path` needs to be a list or path to a file or directory (got '{path}')."
        )
    n_primary = 0
    n_unmapped = 0
    ds_runids = set()
    ds_basecall_models = set()
    for file in target_files:
        if input_type == "fastq":
            with pysam.FastxFile(file) as f:
                for entry in f:
                    name = entry.name
                    run_id = None
                    if bam_headers_in_fastq:
                        if 'RD:Z:' in entry.comment:
                            (run_id,) = re.findall(r"RD:Z:([^\s]+)", entry.comment)
                    elif "runid=" in entry.comment:
                        (run_id,) = re.findall(r"runid=([^\s]+)", entry.comment)
                    names.append(name)
                    if run_id is not None:
                        run_ids.add(run_id)
        else:
            with pysam.AlignmentFile(file, check_sq=False) as f:
                # populate metamap items from RG.DS
                for read_group in f.header.get("RG", []):
                    for ds_kv in read_group.get("DS", "").split():
                        k, v = ds_kv.split("=", 1)
                        if k == "runid":
                            ds_runids.add(v)
                        elif k == "basecall_model":
                            ds_basecall_models.add(v)
                for entry in f:
                    # Just take unmapped reads and primary alignments
                    if entry.is_unmapped:
                        n_unmapped += 1
                    else:
                        if not (entry.is_secondary or entry.is_supplementary):
                            n_primary += 1
                    name = entry.query_name
                    run_id = dict(entry.tags).get("RD")
                    names.append(name)
                    if run_id is not None:
                        run_ids.add(run_id)
    # add n_reads, n_primary or n_unmapped to the dict to be checked later
    prel_meta = dict(
        names=names,
        run_ids=run_ids,
    )
    # if `bam_headers_in_fastq`, `xam_ingress()` was run with `return_fastq: true`
    if input_type == "fastq" or bam_headers_in_fastq:
        prel_meta["n_seqs"] = len(names)
    else:
        prel_meta["n_primary"] = n_primary
        prel_meta["n_unmapped"] = n_unmapped
    # add DS tags for BAM
    if input_type == "bam":
        prel_meta["ds_runids"] = list(ds_runids)
        prel_meta["ds_basecall_models"] = list(ds_basecall_models)

    return prel_meta


def amend_meta_for_output(meta, output_type, chunk_size, ingress_results_dir):
    """Amend the metadata dict for the output type.

    create_preliminary_meta() does double duty for both input and output files.
    This function amends the metadata dict for the output type.
    """
    # additional meta data for fastq output
    if output_type == "fastq":
        meta["n_fastq"] = 1
        if chunk_size is not None:
            meta["n_fastq"] = meta["n_seqs"] // chunk_size + int(meta["n_seqs"] % chunk_size > 0)
        meta["group_key"] = {"groupSize": meta["n_fastq"], "groupTarget": meta["alias"]}
        meta["group_index"] = [meta["alias"] + f"_{i}" for i in range(meta["n_fastq"])]

    # clear some things that aren't present in no-stats cases
    sample_results = ingress_results_dir / meta["alias"]
    if not list(sample_results.glob("*stats*/run_ids")):
        meta["run_ids"] = []
        if output_type == "fastq":
            meta["n_seqs"] = None
        elif output_type == "bam":
            meta["n_primary"] = None
            meta["n_unmapped"] = None

    return meta


def create_metadict(**kwargs):
    """Create dict of metadata and check if required values are present."""
    if "alias" not in kwargs or kwargs["alias"] is None:
        raise ValueError("Meta data needs 'alias'.")
    defaults = dict(barcode=None, type="test_sample", run_ids=[])
    if "run_ids" in kwargs:
        # cast to sorted list to compare to workflow output
        kwargs["run_ids"] = sorted(list(kwargs["run_ids"]))
    defaults.update(kwargs)
    defaults["alias"] = defaults["alias"].replace(" ", "_")
    return defaults


def is_unaligned(path):
    """Check if uBAM.

    When a single file, checks if there are `@SQ` lines in the header. When a directory,
    return `True` if all XAM files are missing `@SQ` lines. If there are mixed headers
    (i.e. some have `@SQ` lines and some don't or the `@SQ` lines between different
    files don't match), blow up.
    """
    if path.is_file():
        target_files = [path]
    elif path.is_dir():
        target_files = get_target_files(path, "bam")
    else:
        raise ValueError("`path` is neither file nor directory.")

    first_sq_lines = None
    for target_file in target_files:
        with pysam.AlignmentFile(target_file, check_sq=False) as f:
            sq_lines = f.header["SQ"]
        if first_sq_lines is None:
            # first file
            first_sq_lines = sq_lines
        else:
            # subsequent file
            if first_sq_lines != sq_lines:
                raise ValueError(f"'{path}' contains (u)BAM files with mixed headers.")
    # if no error was raised, all files had the same `@SQ` files and we can determine
    # `is_unaligned` based on the `@SQ` lines of the first file
    return not first_sq_lines


def get_valid_inputs(input_path, input_type, sample_sheet, chunk_size, params):
    """Get valid input paths and corresponding metadata."""
    # get functions for FASTQ or BAM case
    check_input_type(input_type)
    input_path = Path(input_path)
    # find the valid inputs
    valid_inputs = []

    # handle file case first
    if input_path.is_file():
        # get sequence names and run IDs
        prel_meta = create_preliminary_meta(
            input_path, input_type,
            chunk_size, params["wf"]["return_fastq"]
        )
        del prel_meta['names']
        meta = create_metadict(
            alias=params["sample"]
            if params["sample"] is not None
            else input_path.name.split(".")[0],
            **prel_meta
        )
        valid_inputs.append([meta, input_path])
    else:
        # is a directory --> check if target files in top-level dir or in sub-dirs
        top_dir_target_files = get_target_files(input_path, input_type)
        subdirs_with_target_files = [
            x
            for x in input_path.iterdir()
            if x.is_dir() and get_target_files(x, input_type)
        ]
        if top_dir_target_files and subdirs_with_target_files:
            raise ValueError(
                f"Input directory '{input_path}' cannot contain {input_type.upper()} "
                f"files and sub-directories with {input_type.upper()} files."
            )
        # make sure we only have target files in either (top-level dir or sub-dirs) and
        # not both
        if not top_dir_target_files and not subdirs_with_target_files:
            raise ValueError(
                f"Input directory '{input_path}' contains neither sub-directories "
                f"nor {input_type.upper()} files."
            )
        if top_dir_target_files:
            # get the run IDs of all files
            prel_meta = create_preliminary_meta(
                top_dir_target_files, input_type,
                chunk_size, params["wf"]["return_fastq"]
            )

            del prel_meta['names']
            meta = create_metadict(
                alias=params["sample"]
                if params["sample"] is not None
                else input_path.name,
                **prel_meta
            )
            valid_inputs.append([meta, input_path])
        else:
            # iterate over the sub-directories
            for subdir in subdirs_with_target_files:
                # make sure we don't have sub-sub-directories containing target files
                if any(
                    get_target_files(x, input_type)
                    for x in subdir.iterdir()
                    if x.is_dir()
                ):
                    raise ValueError(
                        f"Input directory '{input_path}' cannot contain more than one "
                        f"level of sub-directories with {input_type.upper()} files."
                    )
                # handle unclassified
                if subdir.name == "unclassified" and not params["analyse_unclassified"]:
                    continue
                # get the run IDs of all files        
                prel_meta = create_preliminary_meta(
                    subdir, input_type,
                    chunk_size, params["wf"]["return_fastq"]
                )
                del prel_meta['names']
                barcode = subdir.name
                meta = create_metadict(
                    alias=barcode,
                    barcode=barcode,
                    **prel_meta
                )
                valid_inputs.append([meta, subdir])
            # parse the sample sheet in case there was one
            if sample_sheet is not None:
                sample_sheet = pd.read_csv(sample_sheet).set_index(
                    # set 'barcode' as index while also keeping the 'barcode' column in
                    # the df
                    "barcode",
                    drop=False,
                )
                # now, get the corresponding inputs for each entry in the sample sheet
                # (sample sheet entries for which no input directory was found will have
                # `None` as their input path in `valid_inputs`); we need a dict mapping
                # barcodes to valid input paths for this
                valid_inputs_dict = {
                    path.name: [meta, path] for meta, path in valid_inputs
                }
                # reset `valid_inputs`
                valid_inputs = []
                for barcode, sample_sheet_entry in sample_sheet.iterrows():
                    try:
                        meta, path = valid_inputs_dict[barcode]
                    except KeyError:
                        meta, path = {}, None
                    meta.update(dict(sample_sheet_entry))
                    valid_inputs.append([create_metadict(**dict(meta)), path])
    # Finally, in case of XAM, loop over the valid inputs again and check if
    # they are uBAM. If so and not `keep_unaligned`, set the path to `None` and
    # the run IDs to `[]`.
    if input_type == "bam":
        valid_inputs_tmp = []
        for meta, path in valid_inputs:
            if path is not None:
                meta["is_unaligned"] = is_unaligned(path)
                if meta.get("is_unaligned") and not params["wf"]["keep_unaligned"]:
                    path = None
                    meta["run_ids"] = []
            valid_inputs_tmp.append([meta, path])
        valid_inputs = valid_inputs_tmp
    return valid_inputs
