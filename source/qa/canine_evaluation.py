import sys
from dataclasses import field, dataclass
from pathlib import Path
from typing import Tuple, Optional

sys.path.append(Path(__file__).parent.parent.parent.as_posix())

from datasets import load_dataset, load_metric, Dataset, load_from_disk
from transformers import CanineForQuestionAnswering, CanineTokenizer, HfArgumentParser
import torch
from torch.utils.data import DataLoader

from source.qa import (
    TokenizedDataset,
    Preprocessor,
    set_seed,
    to_pandas,
    remove_examples_longer_than_threshold,
    evaluate,
)

seed = 0
set_seed(seed)


@dataclass
class EvaluationArguments:
    """
    Arguments needed to evaluating CANINE model on XQUAD dataset (inspired from HuggingFace run_qa.py script)
    """

    model_path: str = field(
        default=None,
        metadata={"help": "Path towards finetuned model on either SQUADv1 or SQUADv2"},
    )
    dataset_name: str = field(
        default=None,
        metadata={
            "help": "Name of the dataset to run evaluation on. Either: xquad or noisy."
        },
    )
    data_dir: str = field(
        default=None,
        metadata={
            "help": "Path to the directory containing the data. Should be set only if `dataset_name`==noisy"
        },
    )
    huggingface_model_checkpoint: str = field(
        default=None,
        metadata={
            "help": "Name of the HuggingFace CANINE model checkpoint to use. Either: google/canine-s "
            "or google/canine-c."
        },
    )
    language: str = field(
        default=None,
        metadata={
            "help": "Language to select in XQUAD dataset, can be one of 'xquad.en', 'xquad.ar', 'xquad.de', "
            "'xquad.zh', 'xquad.vi', 'xquad.es', 'xquad.hi', 'xquad.el', 'xquad.th', 'xquad.tr', 'xquad.ru', "
            "'xquad.ro']"
        },
    )
    squad_v2: bool = field(
        default=None,
        metadata={
            "help": "If true, dataset is similar as SQUADv2 some of the examples do not have an "
            "answer."
        },
    )
    max_length: int = field(
        default=2048,
        metadata={
            "help": "The maximum total input sequence length after tokenization. Sequences longer "
            "than this will be truncated, sequences shorter will be padded."
        },
    )
    doc_stride: int = field(
        default=512,
        metadata={
            "help": "When splitting up a long document into chunks, how much stride to take between chunks."
        },
    )
    n_best_size: int = field(
        default=20,
        metadata={
            "help": "The total number of n-best predictions to generate when looking for an answer."
        },
    )
    max_answer_length: int = field(
        default=256,
        metadata={
            "help": "The maximum length of an answer that can be generated. This is needed because the start "
            "and end predictions are not conditioned on one another."
        },
    )
    batch_size: int = field(
        default=12,
        metadata={"help": "Batch size"},
    )
    device: str = field(
        default="cpu",
        metadata={"help": "Device, either 'cuda' or 'cpu'"},
    )

    def __post_init__(self) -> None:
        if (self.dataset_name == "noisy" and self.data_dir is None) or (
            self.data_dir is not None and self.dataset_name != "noisy"
        ):
            raise ValueError(
                "If you set `dataset_name` to noisy, you must provide the path to the directory containing the noisy "
                "data. If you provide a `data_dir`, then you must have chosen `dataset_name`==noisy"
            )


def evaluate_finetuned_model(
    finetuned_model_path: str,
    dataset_name: str,
    data_dir: Optional[str],
    huggingface_model_checkpoint: str,
    language: str,
    max_answer_length: int,
    n_best_size: int,
    max_length: int,
    doc_stride: int,
    batch_size: int,
    squad_v2: bool,
    device: str,
) -> Tuple[float, float, float, float]:
    if dataset_name == "xquad":
        print(f"Loading dataset {args.language}")
        datasets = load_dataset("xquad", language)
    elif dataset_name == "noisy":
        print("Loading noisy dataset")
        datasets = load_from_disk(data_dir)
    else:
        raise NotImplementedError

    print("Start preprocessing dataset")
    preprocessor = Preprocessor(datasets)
    datasets = preprocessor.preprocess()

    df_validation = to_pandas(datasets["validation"])
    df_validation = remove_examples_longer_than_threshold(
        df_validation, max_length=max_length * 2, doc_stride=doc_stride
    )
    datasets["validation"] = Dataset.from_pandas(df_validation)

    if "length_context" and "length_question" in datasets["validation"].column_names:
        datasets["validation"] = datasets["validation"].remove_columns(
            ["length_context"]
        )
        datasets["validation"] = datasets["validation"].remove_columns(
            ["length_question"]
        )

    # canine tokenizer
    tokenizer = CanineTokenizer.from_pretrained(huggingface_model_checkpoint)
    lang = language.split(".")[-1]
    tokenizer_dataset = TokenizedDataset(
        tokenizer, max_length, doc_stride, squad_v2=squad_v2, language=lang
    )
    print("Start tokenizing dataset")
    tokenized_datasets = datasets.map(
        tokenizer_dataset.tokenize,
        batched=True,
        remove_columns=datasets["validation"].column_names,
    )

    validation_examples = tokenized_datasets["validation"]
    tokenized_datasets["validation"] = tokenized_datasets["validation"].remove_columns(
        ["example_id"]
    )

    metric = load_metric("squad_v2" if squad_v2 else "squad")

    # initialize validation set data loader
    tokenized_datasets["validation"].set_format(
        "torch"
    )  # set into pytorch format for dataloader
    val_loader = DataLoader(
        tokenized_datasets["validation"],
        batch_size=batch_size,
        shuffle=False,
        drop_last=True,
        num_workers=0,
    )

    print("Loading model")
    model = CanineForQuestionAnswering.from_pretrained(huggingface_model_checkpoint)
    model.load_state_dict(torch.load(finetuned_model_path, map_location=device))
    model.to(device)

    print("Start evaluation")
    val_acc, val_loss, f1, exact_match = evaluate(
        model=model,
        val_dataset=datasets["validation"],
        features_val_dataset=validation_examples,
        val_loader=val_loader,
        tokenizer=tokenizer,
        device=device,
        batch_size=batch_size,
        metric=metric,
        max_answer_length=max_answer_length,
        n_best_size=n_best_size,
        squad_v2=squad_v2,
    )

    return val_acc, val_loss, f1, exact_match


if __name__ == "__main__":
    parser = HfArgumentParser(EvaluationArguments)
    args = parser.parse_args_into_dataclasses()[0]

    val_acc, val_loss, f1, exact_match = evaluate_finetuned_model(
        finetuned_model_path=args.model_path,
        dataset_name=args.dataset_name,
        data_dir=args.data_dir,
        huggingface_model_checkpoint=args.huggingface_model_checkpoint,
        language=args.language,
        max_answer_length=args.max_answer_length,
        n_best_size=args.n_best_size,
        max_length=args.max_length,
        doc_stride=args.doc_stride,
        batch_size=args.batch_size,
        squad_v2=args.squad_v2,
        device=args.device,
    )
    print(
        "Validation accuracy:",
        val_acc,
        "Validation loss:",
        val_loss,
        "Validation F1-score:",
        f1,
        "Validation Exact Match:",
        exact_match,
    )
    print(
        "---------------------------------------------------------------------------------------------------------"
    )
    print()
