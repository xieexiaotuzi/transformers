from argparse import ArgumentParser
from os import listdir, makedirs
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from packaging.version import parse

from transformers import is_tf_available, is_torch_available
from transformers.file_utils import ModelOutput
from transformers.pipelines import Pipeline, pipeline
from transformers.tokenization_utils import BatchEncoding


# This is the minimal required version to
# support some ONNX Runtime features
ORT_QUANTIZE_MINIMUM_VERSION = parse("1.4.0")


SUPPORTED_PIPELINES = [
    "feature-extraction",
    "ner",
    "sentiment-analysis",
    "fill-mask",
    "question-answering",
    "text-generation",
    "translation_en_to_fr",
    "translation_en_to_de",
    "translation_en_to_ro",
]


class OnnxConverterArgumentParser(ArgumentParser):
    """
    Wraps all the script arguments supported to export transformers models to ONNX IR
    """

    def __init__(self):
        super().__init__("ONNX Converter")

        self.add_argument(
            "--pipeline", type=str, choices=SUPPORTED_PIPELINES, default="feature-extraction",
        )
        self.add_argument(
            "--model", type=str, required=True, help="Model's id or path (ex: bert-base-cased)",
        )
        self.add_argument("--tokenizer", type=str, help="Tokenizer's id or path (ex: bert-base-cased)")
        self.add_argument(
            "--framework", type=str, choices=["pt", "tf"], help="Framework for loading the model",
        )
        self.add_argument("--opset", type=int, default=11, help="ONNX opset to use")
        self.add_argument(
            "--check-loading", action="store_true", help="Check ONNX is able to load the model",
        )
        self.add_argument(
            "--use-external-format", action="store_true", help="Allow exporting model >= than 2Gb",
        )
        self.add_argument(
            "--quantize", action="store_true", help="Quantize the neural network to be run with int8",
        )
        self.add_argument("output")


def generate_identified_filename(filename: Path, identifier: str) -> Path:
    """
    Append a string-identifier at the end (before the extension,  if any) to the provided filepath.
    Args:
        filename: pathlib.Path The actual path object we would like to add an identifier suffix
        identifier: The suffix to add

    Returns: String with concatenated indentifier at the end of the filename
    """
    return filename.parent.joinpath(filename.stem + identifier).with_suffix(filename.suffix)


def ensure_onnxruntime_installed():
    """
    Check onnxruntime is installed and if the installed version match is recent enough.
    Raises:
        ImportError: If onnxruntime is not installed or too old version is found
    """
    try:
        import onnxruntime

        # Parse the version of the installed onnxruntime
        ort_version = parse(onnxruntime.__version__)

        # We require 1.4.0 minimum
        if ort_version < ORT_QUANTIZE_MINIMUM_VERSION:
            ImportError(
                f"We found an older version of onnxruntime ({onnxruntime.__version__}) "
                f"but we require onnxruntime to be >= 1.4.0 to enable all the conversions options.\n"
                f"Please update onnxruntime by running `pip install --upgrade onnxruntime`"
            )

    except ImportError:
        ImportError(
            "onnxruntime doesn't seem to be currently installed. "
            "Please install the onnxruntime by running `pip install onnxruntime`"
            " and relaunch the conversion."
        )


def ensure_valid_input(model, tokens, input_names):
    """
    Ensure input are presented in the correct order, without any None
    Args:
        model: The model used to forward the input data
        tokens: BatchEncoding holding the input data
        input_names: The name of the inputs

    Returns: Tuple

    """
    print("Ensuring inputs are in correct order")

    model_args_name = model.forward.__code__.co_varnames
    model_args, ordered_input_names = [], []
    for arg_name in model_args_name[1:]:  # start at index 1 to skip "self" argument
        if arg_name in input_names:
            ordered_input_names.append(arg_name)
            model_args.append(tokens[arg_name])
        else:
            print(f"{arg_name} is not present in the generated input list.")
            break

    print("Generated inputs order: {}".format(ordered_input_names))
    return ordered_input_names, tuple(model_args)


def infer_shapes(nlp: Pipeline, framework: str) -> Tuple[List[str], List[str], Dict, BatchEncoding]:
    """
    Attempt to infer the static vs dynamic axes for each input and output tensors for a specific model.
    Args:
        nlp: The pipeline object holding the model to be exported
        framework: The framework identifier to dispatch to the correct inference scheme (pt/tf)

    Returns:
        - List of the inferred input variable names
        - List of the inferred output variable names
        - Dictionary with input/output variables names as key and shape tensor as value
        - a BatchEncoding reference which was used to infer all the above information
    """

    def build_shape_dict(name: str, tensor, is_input: bool, seq_len: int):
        if isinstance(tensor, (tuple, list)):
            return [build_shape_dict(name, t, is_input, seq_len) for t in tensor]

        else:
            # Let's assume batch is the first axis with only 1 element (~~ might not be always true ...)
            axes = {[axis for axis, numel in enumerate(tensor.shape) if numel == 1][0]: "batch"}
            if is_input:
                if len(tensor.shape) == 2:
                    axes[1] = "sequence"
                else:
                    raise ValueError(f"Unable to infer tensor axes ({len(tensor.shape)})")
            else:
                seq_axes = [dim for dim, shape in enumerate(tensor.shape) if shape == seq_len]
                axes.update({dim: "sequence" for dim in seq_axes})

        print(f"Found {'input' if is_input else 'output'} {name} with shape: {axes}")
        return axes

    tokens = nlp.tokenizer("This is a sample output", return_tensors=framework)
    seq_len = tokens.input_ids.shape[-1]
    outputs = nlp.model(**tokens) if framework == "pt" else nlp.model(tokens)
    if isinstance(outputs, ModelOutput):
        outputs = outputs.to_tuple()
    if not isinstance(outputs, (list, tuple)):
        outputs = (outputs,)

    # Generate input names & axes
    input_vars = list(tokens.keys())
    input_dynamic_axes = {k: build_shape_dict(k, v, True, seq_len) for k, v in tokens.items()}

    # flatten potentially grouped outputs (past for gpt2, attentions)
    outputs_flat = []
    for output in outputs:
        if isinstance(output, (tuple, list)):
            outputs_flat.extend(output)
        else:
            outputs_flat.append(output)

    # Generate output names & axes
    output_names = [f"output_{i}" for i in range(len(outputs_flat))]
    output_dynamic_axes = {k: build_shape_dict(k, v, False, seq_len) for k, v in zip(output_names, outputs_flat)}

    # Create the aggregated axes representation
    dynamic_axes = dict(input_dynamic_axes, **output_dynamic_axes)
    return input_vars, output_names, dynamic_axes, tokens


def load_graph_from_args(pipeline_name: str, framework: str, model: str, tokenizer: Optional[str] = None) -> Pipeline:
    """
    Convert the set of arguments provided through the CLI to an actual pipeline reference (tokenizer + model)
    Args:
        pipeline_name: The kind of pipeline to use (ner, question-answering, etc.)
        framework: The actual model to convert the pipeline from ("pt" or "tf")
        model: The model name which will be loaded by the pipeline
        tokenizer: The tokenizer name which will be loaded by the pipeline, defaut to the model's value

    Returns: Pipeline object

    """
    # If no tokenizer provided
    if tokenizer is None:
        tokenizer = model

    # Check the wanted framework is available
    if framework == "pt" and not is_torch_available():
        raise Exception("Cannot convert because PyTorch is not installed. Please install torch first.")
    if framework == "tf" and not is_tf_available():
        raise Exception("Cannot convert because TF is not installed. Please install tensorflow first.")

    print(f"Loading pipeline (model: {model}, tokenizer: {tokenizer})")

    # Allocate tokenizer and model
    return pipeline(pipeline_name, model=model, tokenizer=tokenizer, framework=framework)


def convert_pytorch(nlp: Pipeline, opset: int, output: Path, use_external_format: bool):
    """
    Export a PyTorch backed pipeline to ONNX Intermediate Representation (IR)
    Args:
        nlp: The pipeline to be exported
        opset: The actual version of the ONNX operator set to use
        output: Path where will be stored the generated ONNX model
        use_external_format: Split the model definition from its parameters to allow model bigger than 2GB

    Returns:

    """
    if not is_torch_available():
        raise Exception("Cannot convert because PyTorch is not installed. Please install torch first.")

    import torch
    from torch.onnx import export

    print(f"Using framework PyTorch: {torch.__version__}")

    with torch.no_grad():
        input_names, output_names, dynamic_axes, tokens = infer_shapes(nlp, "pt")
        ordered_input_names, model_args = ensure_valid_input(nlp.model, tokens, input_names)

        export(
            nlp.model,
            model_args,
            f=output.as_posix(),
            input_names=ordered_input_names,
            output_names=output_names,
            dynamic_axes=dynamic_axes,
            do_constant_folding=True,
            use_external_data_format=use_external_format,
            enable_onnx_checker=True,
            opset_version=opset,
        )


def convert_tensorflow(nlp: Pipeline, opset: int, output: Path):
    """
    Export a TensorFlow backed pipeline to ONNX Intermediate Representation (IR)
    Args:
        nlp: The pipeline to be exported
        opset: The actual version of the ONNX operator set to use
        output: Path where will be stored the generated ONNX model

    Notes: TensorFlow cannot export model bigger than 2GB due to internal constraint from TensorFlow

    """
    if not is_tf_available():
        raise Exception("Cannot convert because TF is not installed. Please install tensorflow first.")

    print("/!\\ Please note TensorFlow doesn't support exporting model > 2Gb /!\\")

    try:
        import tensorflow as tf
        from keras2onnx import convert_keras, save_model, __version__ as k2ov

        print(f"Using framework TensorFlow: {tf.version.VERSION}, keras2onnx: {k2ov}")

        # Build
        input_names, output_names, dynamic_axes, tokens = infer_shapes(nlp, "tf")

        # Forward
        nlp.model.predict(tokens.data)
        onnx_model = convert_keras(nlp.model, nlp.model.name, target_opset=opset)
        save_model(onnx_model, output.as_posix())

    except ImportError as e:
        raise Exception(f"Cannot import {e.name} required to convert TF model to ONNX. Please install {e.name} first.")


def convert(
    framework: str,
    model: str,
    output: Path,
    opset: int,
    tokenizer: Optional[str] = None,
    use_external_format: bool = False,
    pipeline_name: str = "feature-extraction",
):
    """
    Convert the pipeline object to the ONNX Intermediate Representation (IR) format.
    Args:
        framework: The framework the pipeline is backed by ("pt" or "tf")
        model: The name of the model to load for the pipeline
        output: The path where the ONNX graph will be stored
        opset: The actual version of the ONNX operator set to use
        tokenizer: The name of the model to load for the pipeline, default to the model's name if not provided
        use_external_format: Split the model definition from its parameters to allow model bigger than 2GB (PyTorch only)
        pipeline_name: The kind of pipeline to instantiate (ner, question-answering, etc.)

    Returns:

    """
    print(f"ONNX opset version set to: {opset}")

    # Load the pipeline
    nlp = load_graph_from_args(pipeline_name, framework, model, tokenizer)

    if not output.parent.exists():
        print(f"Creating folder {output.parent}")
        makedirs(output.parent.as_posix())
    elif len(listdir(output.parent.as_posix())) > 0:
        raise Exception(f"Folder {output.parent.as_posix()} is not empty, aborting conversion")

    # Export the graph
    if framework == "pt":
        convert_pytorch(nlp, opset, output, use_external_format)
    else:
        convert_tensorflow(nlp, opset, output)


def quantize(onnx_model_path: Path) -> Path:
    """
    Quantize the weights of the model from float32 to in8 to allow very efficient inference on modern CPU.
    Args:
        onnx_model_path: Path to location the exported ONNX model is stored

    Returns: The Path generated for the quantized
    """

    try:
        ensure_onnxruntime_installed()
        import onnx
        from onnxruntime import __version__ as ort_version
        from onnxruntime.quantization import quantize, QuantizationMode

        print(f"Found ONNX: {onnx.__version__}")
        print(f"Found ONNXRuntime: {ort_version}")

        onnx_model = onnx.load(onnx_model_path.as_posix())
        quantized_model = quantize(
            model=onnx_model, quantization_mode=QuantizationMode.IntegerOps, force_fusions=True, symmetric_weight=True,
        )

        # Append "-quantized" at the end of the model's name
        quantized_model_path = generate_identified_filename(onnx_model_path, "-quantized")

        # Save model
        print(f"Storing quantized model at {quantized_model_path}")
        onnx.save(quantized_model, quantized_model_path.as_posix())

        return quantized_model_path
    except ImportError as ie:
        print(f"Error while quantizing the model:\n{str(ie)}")


def verify(path: Path):
    from onnxruntime import InferenceSession, SessionOptions
    from onnxruntime.capi.onnxruntime_pybind11_state import RuntimeException

    print(f"Checking ONNX model loading from: {path}")
    try:
        onnx_options = SessionOptions()
        _ = InferenceSession(path.as_posix(), onnx_options, providers=["CPUExecutionProvider"])
        print(f"Model {path} correctly loaded: \N{heavy check mark}")
    except RuntimeException as re:
        print(f"Error while loading the model {re}: \N{heavy ballot x}")


if __name__ == "__main__":
    parser = OnnxConverterArgumentParser()
    args = parser.parse_args()

    # Make sure output is absolute path
    args.output = Path(args.output).absolute()

    try:
        # Convert
        convert(
            args.framework,
            args.model,
            args.output,
            args.opset,
            args.tokenizer,
            args.use_external_format,
            args.pipeline,
        )

        if args.quantize:
            args.quantized_output = quantize(args.output)

        # And verify
        if args.check_loading:
            verify(args.output)

            if hasattr(args, "quantized_output"):
                verify(args.quantized_output)

    except Exception as e:
        print(f"Error while converting the model: {e}")
        exit(1)
